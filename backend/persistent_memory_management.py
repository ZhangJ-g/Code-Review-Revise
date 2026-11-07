"""Persistent Memory Management：管理所有长期记忆，以及当前应用哪一条。

启动时从 JSON 加载到内存；平时从内存读取，用户修改时再保存文件。
"""

import copy
import json
import threading
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol

from .memory_store import MemoryConflict, PersistentMemoryStore, memory_catalog_to_dict, validate_memory
from .models import LongTermMemory, MemorySnapshot


class MemoryProvider(Protocol):
    """记忆读取接口：提供 get_snapshot()，就能让上下文模块取得当前记忆。"""

    def get_snapshot(self) -> MemorySnapshot: ...


class PersistentMemoryManager:
    """保存一个应用实例的全部记忆，所有会话共用。

    RLock 是线程锁：同一时间只让一个请求修改记忆，同一线程可以重复进入。
    返回的快照是固定状态，调用方不能通过它直接修改管理器里的记忆。
    """

    def __init__(self, store: PersistentMemoryStore | None = None) -> None:
        self.store = store if store is not None else PersistentMemoryStore()
        self._lock = threading.RLock()#线程锁
        # revision：记忆目录的修改版本号；active_id：当前应用的记忆 ID。
        self._revision, self._active_id, records = self.store.load()
        self._long_term_memory_cache = {record.id: record for record in records}#内存缓存主体 所有记忆对象
        # 保存已处理请求及其结果，同一请求重试时不重复创建或修改记忆。
        self._receipts = OrderedDict()#防止重复执行操作

    def get_snapshot(self) -> MemorySnapshot:
        #snapshot 快照
        """从内存取一份当前状态（快照），不读取 JSON 文件。"""
        with self._lock:#获取锁、进行操作  #  revision：记忆目录的修改版本号；active_id：当前应用的记忆 ID。  通过字典拿到值
            return MemorySnapshot(self._revision, self._long_term_memory_cache.get(self._active_id))

    def get_active_memory(self) -> LongTermMemory | None:
        """只取当前应用的那条记忆；没有应用任何记忆时返回 None。"""

        return self.get_snapshot().memory

    def list_memories(self) -> dict:
        """把全部记忆、当前应用 ID 和版本号整理成可返回前端的数据。"""
        with self._lock:
            #  revision：记忆目录的修改版本号；active_id：当前应用的记忆 ID。
            return memory_catalog_to_dict(self._revision, self._active_id, self._long_term_memory_cache.values())

    def execute(self, operation: str, *, memory_id=None, name=None, content=None,
                expected_revision: int | None = None, request_id: str | None = None) -> dict:
        """处理新建、修改、删除和应用记忆。

        先检查重复请求和版本，再在副本上修改；文件保存成功后才更新内存。
        这样保存失败时，原来的记忆仍然可用。
        """
        signature = json.dumps([operation, memory_id, name, content, expected_revision], ensure_ascii=False)
        with self._lock:
            # 请求去重：相同 ID 和参数直接返回上次结果；相同 ID 换了参数则报错。
            if request_id in self._receipts:
                previous, result = self._receipts[request_id]
                if signature != previous:
                    raise MemoryConflict("同一请求 ID 不能用于不同的记忆操作")
                return copy.deepcopy(result)
            # 版本检查：用户编辑期间若有人改过目录，拒绝用旧内容覆盖新内容。
            if expected_revision is not None and (
                type(expected_revision) is not int or expected_revision != self._revision
            ):
                raise MemoryConflict("记忆已被更新，请刷新列表；未保存的编辑内容会保留")
            # 先改副本，暂时不影响正在使用的内存数据。
            records, active_id = dict(self._long_term_memory_cache), self._active_id
            now = datetime.now(timezone.utc)
            if operation == "create":
                record = LongTermMemory(name=name.strip() if isinstance(name, str) else name, content=content)
                validate_memory(record)
                records[record.id] = record
                memory_id = record.id
            elif operation in {"update", "delete", "apply"}:
                if operation != "apply" or memory_id is not None:
                    if not isinstance(memory_id, str) or memory_id not in records:
                        raise LookupError("记忆不存在，请刷新列表")
                if operation == "update":
                    record = replace(records[memory_id], name=name.strip() if isinstance(name, str) else name,
                                     content=content, updated_at=now)
                    validate_memory(record)
                    records[memory_id] = record
                elif operation == "delete":
                    del records[memory_id]
                    if active_id == memory_id:
                        active_id = None
                else:
                    active_id = memory_id
            else:
                raise ValueError("不支持的记忆操作")
            # 每次成功修改，目录版本加一；先保存文件，再把新状态交给后续请求。
            revision = self._revision + 1
            self.store.save(revision, active_id, records.values())
            self._revision, self._active_id, self._long_term_memory_cache = revision, active_id, records
            result = self.list_memories()
            result["memory_id"] = memory_id
            if request_id is not None:
                self._receipts[request_id] = signature, copy.deepcopy(result)
                while len(self._receipts) > 512:
                    self._receipts.popitem(last=False)
            return result
