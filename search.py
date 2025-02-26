# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description: main class
"""

import argparse
import concurrent.futures
import datetime
import json
import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from loguru import logger
import uuid


class Language(Enum):
    """支持的语言枚举"""
    EN = "en"
    ZH = "zh"


class SearchBackend(Enum):
    """支持的搜索后端枚举"""
    SEARCHPRO = "SEARCHPRO"
    SERPER = "SERPER"
    DDGS = "DDGS"


class LLMType(Enum):
    """支持的 LLM 类型枚举"""
    ZHIPUAI = "ZHIPUAI"
    OPENAI = "OPENAI"


@dataclass
class Config:
    """配置类"""
    # 搜索相关配置
    backend: SearchBackend = field(default=SearchBackend.SEARCHPRO)
    reference_count: int = field(default=8)
    search_timeout: int = field(default=15)

    # LLM 相关配置
    llm_type: LLMType = field(default=LLMType.ZHIPUAI)
    model: str = field(default="glm-4-flash")
    token_upper_limit: int = field(default=12800)
    token_to_char_ratio: int = field(default=4)
    reduce_token_factor: float = field(default=0.5)

    # 功能开关
    enable_related_questions: bool = field(default=True)
    enable_history: bool = field(default=False)

    # API Keys 和 Endpoints
    api_keys: Dict[str, str] = field(default_factory=dict)
    endpoints: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> 'Config':
        """从环境变量创建配置"""
        config = cls()

        # 搜索后端配置
        config.backend = SearchBackend[os.environ.get("BACKEND", "SEARCHPRO")]

        # LLM 配置
        config.llm_type = LLMType[os.environ.get("LLM_TYPE", "ZHIPUAI")]
        config.model = os.environ.get("LLM_MODEL", "glm-4-flash")

        # 功能开关
        config.enable_related_questions = os.getenv("RELATED_QUESTIONS", "true").lower() == "true"
        config.enable_history = os.getenv("ENABLE_HISTORY", "false").lower() == "true"

        # API Keys
        config.api_keys = {
            "OPENAI": os.environ.get("OPENAI_API_KEY"),
            "ZHIPUAI": os.environ.get("ZHIPUAI_API_KEY"),
            "SERPER": os.environ.get("SERPER_SEARCH_API_KEY"),
        }

        # Endpoints
        config.endpoints = {
            "OPENAI": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            "ZHIPUAI": os.getenv("ZHIPUAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
            "SERPER": SERPER_SEARCH_ENDPOINT,
            "SEARCHPRO": SEARCHPRO_ENDPOINT,
        }

        return config

    def validate(self) -> None:
        """验证配置的有效性"""
        if self.backend == SearchBackend.SEARCHPRO and not self.api_keys.get("ZHIPUAI"):
            raise RuntimeError("ZHIPUAI_API_KEY not set")
        elif self.backend == SearchBackend.SERPER and not self.api_keys.get("SERPER"):
            raise RuntimeError("SERPER_SEARCH_API_KEY not set")


################################################################################
# Constant values for the RAG model.
################################################################################

# Search engine related. You don't really need to change this.
SERPER_SEARCH_ENDPOINT = "https://google.serper.dev/search"
SEARCHPRO_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/tools"

# Specify the number of references from the search engine you want to use.
# 8 is usually a good number.
REFERENCE_COUNT = 8

# Specify the default timeout for the search engine. If the search engine
# does not respond within this time, we will return an error.
DEFAULT_SEARCH_ENGINE_TIMEOUT = 5


class PromptManager:
    """提示词管理类"""

    def __init__(self):
        self._prompts = {
            "rag_system": {
                Language.EN: """You are a large language AI assistant. You are given a user question, and please write clean, concise and accurate answer to the question. You will be given a set of related contexts to the question, each starting with a reference number like [[citation:x]], where x is a number. Please use the context and cite the context at the end of each sentence if applicable.

Your answer must be correct, accurate and written by an expert using an unbiased and professional tone. Please keep your answer within 1024 tokens. If the provided context does not offer enough information, please use your own knowledge to answer the user question.

Please cite the contexts with the reference numbers, in the format [citation:x]. If a sentence comes from multiple contexts, please list all applicable citations, like [citation:3][citation:5]. Other than code and specific names and citations, your answer must be written in the same language as the question.
""",
                Language.ZH: """你是一个大型的语言AI助手。当用户提出问题时，请你写出清晰、简洁且准确的答案。我们会给你一组与问题相关的上下文，每个上下文都以类似[[citation:x]]这样的引用编号开始，其中x是一个数字。如果有引用[context]，请在每句话后面使用并引述该上下文。

你的答案必须正确、精确，并由专家以公正和专业的语气撰写。请将你的回答限制在1024个token内。如果所提供的上下文信息不足，可以使用自己知识来回答用户问题。

请按照[citation:x]格式引用带有参考编号的上下文。如果一句话来自多个上下文，请列出所有适用于此处引述，如[citation:3][citation:5]。除代码、特定名称和引述外，你必须使用与问题相同语言编写你的回答。
"""
            },
            "rag_qa": {
                Language.EN: """[context]=```
{context}
```
Current date: {current_date}

Please answer the question with contexts, but don't blindly repeat the contexts verbatim. Please cite the contexts with the reference numbers, in the format [citation:x]. And here is the user question:
""",
                Language.ZH: """[context]=```
{context}
```
当前日期: {current_date}

基于上下文回答问题，不要盲目地逐字重复上下文。请以[citation:x]的格式引用上下文。这是用户的问题：
"""
            },
            "related_system": {
                Language.EN: """You assist users in posing relevant questions based on their original queries and related background. Please identify topics worth following up on, and write out questions that each do not exceed 10 tokens. You Can combine with historical messages.""",
                Language.ZH: """你帮助用户根据他们的原始问题和相关背景提出相关问题，可以结合历史消息。请确定值得跟进的主题，每个问题不超过10个token。"""
            },
            "related_qa": {
                Language.EN: """[context]=```
{context}
```

based on the original question and related contexts, suggest three such further questions. Do NOT repeat the original question. Each related question should be no more than 10 tokens, separate 3 questions with `\\n`, do not add numbers before questions, just give question contents. Here is the original question:
""",
                Language.ZH: """[context]=```
{context}
```

根据原始问题和相关上下文，提出三个相似的问题。不要重复原始问题。每个相关问题应不超过10个token，问题前不要加序号，问题后加问号，`\\n`分隔多个问题。这是原始问题：
"""
            },
        }

    def get_prompt(self, prompt_type: str, lang: Language, **kwargs) -> str:
        """获取提示词
        
        Args:
            prompt_type: 提示词类型
            lang: 语言
            **kwargs: 格式化参数
        
        Returns:
            格式化后的提示词
        """
        prompt = self._prompts.get(prompt_type, {}).get(lang, "")
        if not prompt:
            raise ValueError(f"Prompt not found for type {prompt_type} and language {lang}")

        if kwargs:
            prompt = prompt.format(**kwargs)

        return prompt

    def detect_language(self, text: str) -> Language:
        """检测文本语言
        
        Args:
            text: 待检测文本
        
        Returns:
            检测到的语言
        """
        return Language.ZH if any('\u4e00' <= char <= '\u9fff' for char in text) else Language.EN


async def search_with_ddgs(query: str):
    """Search with DuckDuckGo"""
    try:
        from duckduckgo_search import AsyncDDGS

        contexts = []
        async with AsyncDDGS() as ddgs:
            results = await ddgs.text(
                query,
                backend="lite",
                timelimit="d, w, m, y",
                max_results=REFERENCE_COUNT
            )

            for result in results:
                if result.get("body") and result.get("href"):
                    contexts.append({
                        "name": result["title"],
                        "url": result["href"],
                        "snippet": result["body"]
                    })

            return contexts

    except Exception as e:
        logger.error(f"Error in DDGS search: {e}")
        return []


async def search_with_serper(query: str, subscription_key: str):
    """Search with Serper API"""
    payload = json.dumps({
        "q": query,
        "num": REFERENCE_COUNT if REFERENCE_COUNT % 10 == 0 else (REFERENCE_COUNT // 10 + 1) * 10
    }, ensure_ascii=False)

    headers = {
        "X-API-KEY": subscription_key,
        "Content-Type": "application/json"
    }

    logger.info(f"Serper request: {payload}")

    async with httpx.AsyncClient(timeout=DEFAULT_SEARCH_ENGINE_TIMEOUT) as client:
        response = await client.post(
            SERPER_SEARCH_ENDPOINT,
            headers=headers,
            content=payload
        )

    if not response.is_success:
        logger.error(f"{response.status_code} {response.text}")
        raise HTTPException(response.status_code, "Search engine error.")

    json_content = response.json()
    try:
        contexts = []

        # 处理 knowledge graph
        if kg := json_content.get("knowledgeGraph"):
            if url := (kg.get("descriptionUrl") or kg.get("website")):
                if snippet := kg.get("description"):
                    contexts.append({
                        "name": kg.get("title", ""),
                        "url": url,
                        "snippet": snippet
                    })

        # 处理 answer box
        if ab := json_content.get("answerBox"):
            if url := ab.get("url"):
                if snippet := (ab.get("snippet") or ab.get("answer")):
                    contexts.append({
                        "name": ab.get("title", ""),
                        "url": url,
                        "snippet": snippet
                    })

        # 处理普通结果
        contexts.extend([
            {
                "name": c["title"],
                "url": c["link"],
                "snippet": c.get("snippet", "")
            }
            for c in json_content.get("organic", [])
        ])

        logger.debug(f"Serper search results: {contexts[:REFERENCE_COUNT]}")
        return contexts[:REFERENCE_COUNT]

    except KeyError as e:
        logger.error(f"Error parsing Serper response: {e}\n{json_content}")
        return []


async def search_with_searchpro(query: str, api_key: str = None):
    """
    使用智谱 AI 的搜索工具进行搜索
    """
    try:
        api_key = api_key or os.environ.get("ZHIPUAI_API_KEY")
        if not api_key:
            raise RuntimeError("ZHIPUAI_API_KEY not set")
        logger.info(f"Searching web for: {query}")
        msg = [{"role": "user", "content": query}]
        data = {
            "request_id": str(uuid.uuid4()),
            "tool": "web-search-pro",
            "stream": False,
            "messages": msg
        }

        async with httpx.AsyncClient(timeout=DEFAULT_SEARCH_ENGINE_TIMEOUT) as client:
            response = await client.post(
                SEARCHPRO_ENDPOINT,
                json=data,
                headers={'Authorization': api_key}
            )

        if not response.is_success:
            logger.error(f"{response.status_code} {response.text}")
            raise HTTPException(response.status_code, "Search engine error.")

        json_content = response.json()
        try:
            search_results = json_content['choices'][0]['message']['tool_calls'][1]['search_result']
            contexts = []

            for result in search_results[:REFERENCE_COUNT]:
                contexts.append({
                    "name": result.get("title", ""),
                    "url": result.get("link", ""),
                    "snippet": result.get("content", "")
                })
            logger.debug(f"Search results: {contexts}")
            return contexts
        except KeyError:
            logger.error(f"Error encountered: {json_content}")
            return []
    except Exception as e:
        logger.error(f"Failed to search with SearchPro: {e}")
        return []


class SearchRequest(BaseModel):
    """搜索请求模型"""
    query: str
    search_uuid: Optional[str] = None


class GenerateAnswerRequest(BaseModel):
    """生成回答请求模型"""
    query: str
    contexts: List[dict]
    search_uuid: Optional[str] = None


class RelatedQuestionsRequest(BaseModel):
    """相关问题请求模型"""
    query: str
    contexts: List[dict]
    search_uuid: Optional[str] = None


class SearchAPI(FastAPI):
    """
    Retrieval-Augmented Generation Search API
    
    基于搜索引擎和LLM的智能搜索服务
    """

    def __init__(self):
        super().__init__()
        self.init_config()
        self.init_middleware()
        self.init_routes()
        self.init_components()

    def init_config(self):
        """初始化配置"""
        self.config = Config.from_env()
        self.config.validate()

    def init_middleware(self):
        """初始化中间件"""
        self.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    def init_routes(self):
        """初始化API路由"""
        # 静态文件服务
        self.mount("/ui", StaticFiles(directory="ui"), name="ui")

        # API endpoints
        self.post("/search")(self.search)
        self.post("/generate")(self.generate_answer)
        self.post("/related")(self.get_related)
        self.get("/")(self.index)

    def init_components(self):
        """初始化组件"""
        # 提示词管理器
        self.prompt_manager = PromptManager()

        # 线程池
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

        # 历史记录
        self.history = []
        self.related_history = []

        # 搜索函数映射
        self._init_search_function()

    def _init_search_function(self):
        """初始化搜索函数"""
        if self.config.backend == SearchBackend.SEARCHPRO:
            self.search_function = lambda q: search_with_searchpro(q, self.config.api_keys["ZHIPUAI"])
        elif self.config.backend == SearchBackend.SERPER:
            self.search_function = lambda q: search_with_serper(q, self.config.api_keys["SERPER"])
        elif self.config.backend == SearchBackend.DDGS:
            self.search_function = search_with_ddgs
        else:
            raise RuntimeError(f"Unsupported search backend: {self.config.backend}")

    def local_client(self):
        """获取线程本地的 OpenAI 客户端"""
        import openai
        thread_local = threading.local()

        try:
            return thread_local.client
        except AttributeError:
            if self.config.llm_type == LLMType.ZHIPUAI:
                base_url = self.config.endpoints["ZHIPUAI"]
                api_key = self.config.api_keys["ZHIPUAI"]
            elif self.config.llm_type == LLMType.OPENAI:
                base_url = self.config.endpoints["OPENAI"]
                api_key = self.config.api_keys["OPENAI"]
            else:
                raise ValueError(f"Not support {self.config.llm_type}")
            thread_local.client = openai.AsyncOpenAI(
                base_url=base_url,
                api_key=api_key,
            )
            return thread_local.client

    def reduce_tokens(self, history: List[dict]) -> List[dict]:
        """如果token占用太高，移除早期历史记录"""
        history_content_lens = [len(i.get("content", "").replace(" ", "")) for i in history if i]
        if len(history) > 5 and sum(
                history_content_lens) / self.config.token_to_char_ratio > self.config.token_upper_limit:
            count = 0
            while (
                    sum(history_content_lens) / self.config.token_to_char_ratio >
                    self.config.token_upper_limit * self.config.reduce_token_factor
                    and sum(history_content_lens) > 0
            ):
                count += 1
                del history[1:3]
                history_content_lens = [len(i.get("content", "").replace(" ", "")) for i in history if i]
            logger.warning(f"To prevent token over-limit, model forgotten the early {count} turns history.")
        return history

    async def get_related_questions(self, query: str, contexts: List[dict]) -> List[str]:
        """获取相关问题"""
        try:
            lang = self.prompt_manager.detect_language(query)
            context = ""
            if contexts:
                context = "\n\n".join([c["snippet"][:50] for c in contexts[:3]])
            qa_prompt = self.prompt_manager.get_prompt(
                "related_qa",
                lang,
                context=context
            )
            user_prompt = f"{qa_prompt}\n\n{query}"
            logger.debug(f"related prompt: {user_prompt}")

            client = self.local_client()
            response = await client.chat.completions.create(
                model=self.config.model,
                messages=self.related_history + [{"role": "user", "content": user_prompt}],
                max_tokens=512,
            )

            self.related_history = self.reduce_tokens(self.related_history)
            self.related_history.append({"role": "user", "content": query})

            # 解析回答生成相关问题
            answer = response.choices[0].message.content
            # \n分隔，或者？或者?分隔
            questions = [q.strip() for q in answer.split('\n') if q.strip()]
            if len(questions) == 1:
                questions = [q + '？' for q in answer.split('？') if q.strip()]
            if len(questions) == 1:
                questions = [q + '?' for q in answer.split('?') if q.strip()]
            r = questions[:5]
            logger.debug(f"Related questions: {r}")
            return r
        except Exception as e:
            logger.error(f"Error in generating related questions: {e}")
            return []

    async def search(self, request: SearchRequest):
        """搜索服务 - 返回搜索结果上下文"""
        try:
            contexts = await self.search_function(request.query)
            return JSONResponse(content=contexts)
        except Exception as e:
            logger.error(f"Error in search: {e}")
            return JSONResponse(
                content={"error": str(e)},
                status_code=503
            )

    async def generate_answer(self, request: GenerateAnswerRequest) -> StreamingResponse:
        """LLM生成服务 - 流式返回生成的回答"""
        try:
            if not self.config.enable_history:
                self.history = []

            # 检测语言
            lang = self.prompt_manager.detect_language(request.query)

            # 生成回答
            if not self.history:
                content = self.prompt_manager.get_prompt("rag_system", lang)
                self.history.append({"role": "system", "content": content})
            context = ""
            if request.contexts:
                context = "\n\n".join(
                    [f"[[citation:{i + 1}]] {c.get('snippet', '')}" for i, c in enumerate(request.contexts)])
            prompt = self.prompt_manager.get_prompt(
                "rag_qa",
                lang,
                context=context,
                current_date=datetime.datetime.today().strftime("%Y-%m-%d")
            )

            messages = self.history + [{"role": "user", "content": f"{prompt}\n\n{request.query}"}]

            return StreamingResponse(
                self._generate_answer_stream(messages),
                media_type="text/event-stream"
            )

        except Exception as e:
            logger.error(f"Error in generate: {e}")
            return JSONResponse(
                content={"error": str(e)},
                status_code=503
            )

    async def get_related(self, request: RelatedQuestionsRequest):
        """相关问题生成服务"""
        try:
            if not self.config.enable_history:
                self.related_history = []

            if not self.related_history:
                content = self.prompt_manager.get_prompt(
                    "related_system",
                    self.prompt_manager.detect_language(request.query)
                )
                self.related_history.append({"role": "system", "content": content})

            questions = await self.get_related_questions(request.query, request.contexts)
            return JSONResponse(content=questions)
        except Exception as e:
            logger.error(f"Error in related: {e}")
            return JSONResponse(
                content={"error": str(e)},
                status_code=503
            )

    async def _generate_answer_stream(self, messages):
        """生成回答的流式响应"""
        completion = None
        try:
            client = self.local_client()
            completion = await client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                max_tokens=2048,
                stream=True
            )

            response_text = ""
            async for chunk in completion:
                if chunk.choices and chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    response_text += content
                    yield content

            # 保存到历史记录
            if response_text:
                self.history.append({"role": "assistant", "content": response_text})
                logger.debug(f"history: {self.history}")

        except Exception as e:
            logger.error(f"Error in stream response: {e}")
            yield f"Error: {str(e)}"
        finally:
            if completion:
                try:
                    await completion.close()
                except Exception as e:
                    logger.error(f"Error closing completion stream: {e}")

    async def index(self):
        """重定向到UI页面"""
        return RedirectResponse(url="/ui/index.html")


def create_app():
    """创建FastAPI应用实例"""
    return SearchAPI()


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Search API Server")
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port)
