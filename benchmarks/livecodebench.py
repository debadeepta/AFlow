import asyncio
import json
import os
import multiprocessing
import threading
import time
import base64
import zlib
import pickle
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiofiles
import numpy as np
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed
from tqdm import tqdm

from benchmarks.benchmark import BaseBenchmark
from scripts.logs import logger
import sys
sys.path.append("..")
sys.path.append("benchmarks")
from scripts.utils.lcb_test import run_test  # 确保已安装lcb_runner

# 从LiveCodeBench官方eval中复制的关键函数
#sys.set_int_max_str_digits(50000)
os.environ["TOKENIZERS_PARALLELISM"] = "false"

def _temp_run(sample, generation, debug, result, metadata_list, timeout):
    res, metadata = run_test(sample, test=generation, debug=debug, timeout=timeout)
    result.append(res)
    metadata_list.append(metadata)

def check_correctness(sample, generation, timeout, debug=True):
    manager = multiprocessing.Manager()
    result = manager.list()
    metadata_list = manager.list()
    p = multiprocessing.Process(
        target=_temp_run,
        args=(sample, generation, debug, result, metadata_list, timeout),
    )
    p.start()
    p.join(
        timeout=(timeout + 1) * len(json.loads(sample["input_output"])["inputs"]) + 5
    )
    if p.is_alive():
        p.kill()
    if not result:
        in_outs = json.loads(sample["input_output"])
        result = [[-1 for _ in range(len(in_outs["inputs"]))]]
        if debug:
            logger.warning(f"全局超时: {sample.get('task_id', 'unknown')}")

    return result[0], metadata_list[0]

def evaluate_generations_by_problem(args):
    problem_generations, sample, debug, timeout = args
    res = []
    metadata = []
    for generation in problem_generations:
        curr_res = [-2]
        try:
            curr_res, curr_metadata = check_correctness(
                sample, generation, timeout=timeout, debug=debug
            )
            fixed = []
            for e in curr_res:
                if isinstance(e, np.ndarray):
                    e = e.item(0)
                if isinstance(e, np.bool_):
                    e = bool(e)
                fixed.append(e)
            curr_res = fixed
        except Exception as e:
            curr_metadata = {
                "error": repr(e),
                "error_code": -5,
                "error_message": "TestRunnerError",
            }
        finally:
            res.append(curr_res)
            metadata.append(curr_metadata)
    return res, metadata

class LiveCodeBench(BaseBenchmark):
    def __init__(self, name: str, file_path: str, log_path: str, timeout: int = 6):
        super().__init__(name, file_path, log_path)
        self.timeout = timeout
        self.num_process_evaluate = min(16, os.cpu_count() or 4)

    class TimeoutError(Exception):
        pass

    def run_with_timeout(self, func, args, timeout):
        result = []
        exception_occurred = []
        stop_event = threading.Event()

        def target():
            try:
                return_value = func(*args)
                result.append(return_value)
            except Exception as e:
                exception_occurred.append(e)
            finally:
                stop_event.set()

        thread = threading.Thread(target=target)
        thread.start()
        is_timeout = not stop_event.wait(timeout)

        if is_timeout:
            raise self.TimeoutError("Function execution timed out")
        if exception_occurred:
            raise exception_occurred[0]
        return result[0] if result else None
    def parse_code(self,prediction):
        prediction = prediction.split("```python")[-1]
        prediction = prediction.split("```")[0]
        return prediction
    async def load_data(self, specific_indices: List[int] = None) -> List[dict]:
        """从JSONL文件加载数据并转换为LiveCodeBench评测格式"""
        raw_data = []
        async with aiofiles.open(self.file_path, mode="r", encoding="utf-8") as file:
            async for line in file:
                raw_data.append(json.loads(line))
        
        # 转换为评测格式
        processed_data = []
        for item in raw_data:
            try:
                # 处理私有测试用例（只使用private test cases进行评测）
                try:
                    private_tests = json.loads(item["private_test_cases"])
                except:
                    private_tests = json.loads(
                        pickle.loads(
                            zlib.decompress(
                                base64.b64decode(item["private_test_cases"].encode("utf-8"))
                            )
                        )
                    )
                
                # 构建评测样本
                processed_item = {
                    "question": item["question_content"],
                    "input_output": json.dumps({
                        "inputs": [t["input"] for t in private_tests],
                        "outputs": [t["output"] for t in private_tests],
                        "fn_name": json.loads(item["metadata"]).get("func_name", None) if item["metadata"] else None
                    }),
                    "task_id": f"{item['contest_id']}_{item['question_id']}",
                    "canonical_solution": item.get("starter_code", ""),
                    "metadata": {
                        "difficulty": item.get("difficulty", "unknown"),
                        "platform": item.get("platform", "unknown"),
                        "original_data": item  # 保留原始数据
                    }
                }
                processed_data.append(processed_item)   
            
            except Exception as e:
                logger.error(f"处理数据时出错: {str(e)}")
                continue
        
        if specific_indices is not None:
            return [processed_data[i] for i in specific_indices if i < len(processed_data)]
        return processed_data

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(2), retry=retry_if_exception_type(Exception), reraise=True)
    async def _generate_output(self, agent: Callable, prompt: str, entry_point:str) -> Tuple[str, float]:
        # entry_point = "" # 要写func name
        return await asyncio.wait_for(agent(prompt, entry_point), timeout=120)

    async def evaluate_problem(self, problem: dict, agent: Callable, save_path: str = None) -> Tuple[str, str, str, float, Dict, float]:
        question = problem["question"]
        task_id = problem["task_id"]
        
        try:
            logger.info(f"开始评估 LiveCodeBench 问题: {task_id}")
            
            # 生成代码
            entry_point = problem["metadata"].get("func_name", "wrapped_function") if problem["metadata"] else "wrapped_function"
            logger.info(f"entry_point: {entry_point}")
            prediction, cost = await self._generate_output(agent, question, entry_point)
            logger.info(f"完成代码生成，任务: {task_id}, 成本: {cost}")
            prediction = self.parse_code(prediction)
            # 使用LiveCodeBench的评测逻辑
            sample = {
                "question": question,
                "input_output": problem["input_output"],
                "task_id": task_id
            }
            #logger.info(f"开始评估sample {sample['input_output']}")
            
            # 在多进程环境中评估
            args = ([prediction], sample, False, self.timeout)
            loop = asyncio.get_running_loop()
            with ProcessPoolExecutor(max_workers=1) as executor:
                results, metadata = await loop.run_in_executor(
                    executor, evaluate_generations_by_problem, args
                )
            
            # 解析结果
            logger.info(f"测试结果：{results}")
            test_results = results[0]  # 取第一个(也是唯一一个)生成结果的所有测试用例结果
            test_metadata = metadata[0]
            passed = all(r == 1 for r in test_results)
            score = 1.0 if passed else 0.0
            
            # 构建结果详情
            evaluation_details = {
                "task_id": task_id,
                "test_results": test_results,
                "metadata": test_metadata,
                "execution_success": passed,
                "difficulty": problem.get("metadata", {}).get("difficulty", "unknown"),
                "platform": problem.get("metadata", {}).get("platform", "unknown")
            }
            
            # 构建预期输出（用于日志）
            expected_output = {
                "task_id": task_id,
                "difficulty": evaluation_details["difficulty"],
                "platform": evaluation_details["platform"],
                "canonical_solution": problem.get("canonical_solution", "")
            }

            # 记录失败情况
            if not passed:
                self.log_mismatch(
                    problem=question,
                    expected_output=json.dumps(expected_output),
                    prediction=prediction,
                    extracted_output=prediction,
                    extract_answer_code="N/A"
                )
                logger.warning(f"任务失败: {task_id}, 得分: {score}")
            else:
                logger.info(f"任务成功: {task_id}, 得分: {score}")

            result = (question, prediction, json.dumps(expected_output), score, evaluation_details, cost)
            
            # 保存结果
            if save_path:
                async with aiofiles.open(save_path, mode="a", encoding="utf-8") as file:
                    await file.write(json.dumps(result) + "\n")
            
            return result

        except asyncio.TimeoutError:
            logger.error(f"代码生成超时: {task_id}")
            evaluation_details = {"task_id": task_id, "error": "Timeout"}
            return (question, "Timeout", "", 0.0, evaluation_details, 0.0)
        
        except Exception as e:
            import traceback
            traceback.print_exc()
            logger.error(f"评估出错: {task_id}, 错误: {e}")
            evaluation_details = {"task_id": task_id, "error": str(e)}
            return (question, f"评估出错: {str(e)}", "", 0.0, evaluation_details, 0.0)

    def calculate_score(self, expected_output: str, prediction: str) -> Tuple[float, str]:
        return 0.0, ""

    def get_result_columns(self) -> List[str]:
        return ["question", "prediction", "expected_output", "score", "evaluation_details", "cost"]

    async def run_baseline_with_load_data(self, agent: Callable, past_data_path: str = None, max_concurrent_tasks: int = 10):
        all_data = await self.load_data()
        
        if not past_data_path:
            past_data_path = os.path.join(self.log_path, f"{self.name}_results.jsonl")
        
        # 加载历史结果
        past_results = {}
        if os.path.exists(past_data_path):
            async with aiofiles.open(past_data_path, mode="r", encoding="utf-8") as file:
                async for line in file:
                    try:
                        result = json.loads(line)
                        # 使用问题内容作为键
                        past_results[result[0]] = result
                    except:
                        continue

        # 过滤新问题
        new_data = [p for p in all_data if p["question"] not in past_results]
        
        if not new_data:
            logger.info("所有问题都已评估完成")
            return None, None, None

        logger.info(f"发现 {len(new_data)} 个新问题需要评估，共 {len(all_data)} 个问题")

        # 评估新问题
        new_results = await self.evaluate_all_problems(
            new_data, agent, save_path=past_data_path, max_concurrent_tasks=max_concurrent_tasks
        )
        
        # 合并结果
        all_results = list(past_results.values()) + new_results
        
        # 保存最终结果
        columns = self.get_result_columns()
        average_score, average_cost, total_cost = self.save_results_to_csv(all_results, columns)
        
        logger.info(f"{self.name} 数据集平均得分: {average_score:.5f}")
        logger.info(f"总成本: {total_cost:.5f}")
        logger.info(f"平均成本: {average_cost:.5f}")
        return average_score, average_cost, total_cost