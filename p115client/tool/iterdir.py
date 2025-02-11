#!/usr/bin/env python3
# encoding: utf-8

__author__ = "ChenyangGao <https://chenyanggao.github.io>"
__all__ = [
    "ID_TO_DIRNODE_CACHE", "P115ID", "unescape_115_charref", "type_of_attr", "get_path_to_cid", 
    "get_ancestors_to_cid", "get_id_to_path", "get_id_to_sha1", "get_id_to_pickcode", "filter_na_ids", 
    "iter_stared_dirs_raw", "iter_stared_dirs", "ensure_attr_path", "ensure_attr_path_by_category_get", 
    "iterdir_raw", "iterdir", "iter_files", "iter_files_raw", "traverse_files", "iter_dupfiles", 
    "iter_image_files", "iter_dangling_files", "share_extract_payload", "share_iterdir", "share_iter_files", 
    "iter_selected_nodes", "iter_selected_nodes_by_category_get", "iter_selected_nodes_by_edit", 
    "iter_selected_nodes_using_star_event", "iter_selected_dirs_using_star", 
]
__doc__ = "这个模块提供了一些和目录信息罗列有关的函数"

from asyncio import Lock as AsyncLock
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable, Collection, Coroutine, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from errno import EIO, ENOENT
from functools import partial
from itertools import chain, count, islice, takewhile
from operator import itemgetter
from re import compile as re_compile
from string import hexdigits
from threading import Lock
from time import time
from types import EllipsisType
from typing import cast, overload, Any, Final, Literal, NamedTuple, TypedDict
from warnings import warn
from weakref import WeakValueDictionary

from asynctools import async_chain, async_filter, async_map, to_list
from concurrenttools import taskgroup_map, threadpool_map
from iterutils import async_foreach, ensure_aiter, run_gen_step, run_gen_step_iter, through, async_through, Yield, YieldFrom
from iter_collect import grouped_mapping, grouped_mapping_async, iter_keyed_dups, iter_keyed_dups_async, SupportsLT
from orjson import loads
from p115client import check_response, normalize_attr, DataError, P115Client, P115OSError, P115Warning
from p115client.const import CLASS_TO_TYPE, SUFFIX_TO_TYPE
from p115client.type import P115DictAttrLike
from posixpatht import escape, path_is_dir_form, splitext, splits

from .edit import update_desc, update_star
from .fs_files import iter_fs_files, iter_fs_files_threaded, iter_fs_files_asynchronized
from .life import iter_life_behavior_once, life_show


CRE_SHARE_LINK_search1 = re_compile(r"(?:/s/|share\.115\.com/)(?P<share_code>[a-z0-9]+)\?password=(?:(?P<receive_code>[a-z0-9]{4}))?").search
CRE_SHARE_LINK_search2 = re_compile(r"(?P<share_code>[a-z0-9]+)(?:-(?P<receive_code>[a-z0-9]{4}))?").search
CRE_115_CHARREF_sub = re_compile("\\[\x02([0-9]+)\\]").sub


class DirNode(NamedTuple):
    name: str
    parent_id: int


@dataclass(frozen=True, unsafe_hash=True)
class OverviewAttr:
    is_dir: bool
    id: int
    parent_id: int
    name: str
    ctime: int
    mtime: int
    def __getitem__(self, key, /):
        try:
            return getattr(self, key)
        except AttributeError as e:
            raise LookupError(key) from e


#: 用于缓存每个用户（根据用户 id 区别）的每个目录 id 到所对应的 (名称, 父id) 的元组的字典的字典
ID_TO_DIRNODE_CACHE: Final[defaultdict[int, dict[int, tuple[str, int] | DirNode]]] = defaultdict(dict)


class SharePayload(TypedDict):
    share_code: str
    receive_code: None | str


def _overview_attr(info: Mapping, /) -> OverviewAttr:
    if "n" in info:
        is_dir = "fid" not in info
        name = info["n"]
        if is_dir:
            id = int(info["cid"])
            pid = int(info["pid"])
        else:
            id = int(info["fid"])
            pid = int(info["cid"])
        ctime = int(info["tp"])
        mtime = int(info["te"])
    elif "fn" in info:
        is_dir = info["fc"] == "0"
        name = info["fn"]
        id = int(info["fid"])
        pid = int(info["pid"])
        ctime = int(info["uppt"])
        mtime = int(info["upt"])
    elif "file_category" in info:
        is_dir = int(info["file_category"]) == 0
        if is_dir:
            name = info["category_name"]
            id = int(info["category_id"])
            pid = int(info["parent_id"])
            ctime = int(info["pptime"])
            mtime = int(info["ptime"])
        else:
            name = info["file_name"]
            id = int(info["file_id"])
            pid = int(info["category_id"])
            ctime = int(info["user_pptime"])
            mtime = int(info["user_ptime"])
    else:
        raise ValueError(f"can't overview attr data: {info!r}")
    return OverviewAttr(is_dir, id, pid, name, ctime, mtime)


def unescape_115_charref(s: str, /) -> str:
    """对 115 的字符引用进行解码

    :example:

        .. code:: python

            unescape_115_charref("[\x02128074]0号：优质资源") == "👊0号：优质资源"
    """
    return CRE_115_CHARREF_sub(lambda a: chr(int(a[1])), s)


def type_of_attr(attr: Mapping, /) -> int:
    """推断文件信息所属类型（试验版，未必准确）

    :param attr: 文件信息

    :return: 返回类型代码

        - 0: 目录
        - 1: 文档
        - 2: 图片
        - 3: 音频
        - 4: 视频
        - 5: 压缩包
        - 6: 应用
        - 7: 书籍
        - 99: 其它文件
"""
    if attr.get("is_dir") or attr.get("is_directory"):
        return 0
    type: None | int
    if type := CLASS_TO_TYPE.get(attr.get("class", "")):
        return type
    if type := SUFFIX_TO_TYPE.get(splitext(attr["name"])[1].lower()):
        return type
    if attr.get("is_video") or "defination" in attr:
        return 4
    return 99


@overload
def get_path_to_cid(
    client: str | P115Client, 
    cid: int = 0, 
    root_id: None | int = None, 
    escape: None | Callable[[str], str] = escape, 
    refresh: bool = False, 
    id_to_dirnode: None | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> str:
    ...
@overload
def get_path_to_cid(
    client: str | P115Client, 
    cid: int = 0, 
    root_id: None | int = None, 
    escape: None | Callable[[str], str] = escape, 
    refresh: bool = False, 
    id_to_dirnode: None | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, str]:
    ...
def get_path_to_cid(
    client: str | P115Client, 
    cid: int = 0, 
    root_id: None | int = None, 
    escape: None | Callable[[str], str] = escape, 
    refresh: bool = False, 
    id_to_dirnode: None | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> str | Coroutine[Any, Any, str]:
    """获取目录对应的路径（绝对路径或相对路径）

    :param client: 115 客户端或 cookies
    :param cid: 目录的 id
    :param root_id: 根目录 id，如果指定此参数且不为 None，则返回相对路径，否则返回绝对路径
    :param escape: 对文件名进行转义的函数。如果为 None，则不处理；否则，这个函数用来对文件名中某些符号进行转义，例如 "/" 等
    :param refresh: 是否刷新。如果为 True，则会执行网络请求以查询；如果为 False，则直接从 `id_to_dirnode` 中获取
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典
    :param app: 使用某个 app （设备）的接口
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 目录对应的绝对路径或相对路径
    """
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    if id_to_dirnode is None:
        id_to_dirnode = ID_TO_DIRNODE_CACHE[client.user_id]
    def gen_step():
        nonlocal cid
        parts: list[str] = []
        if cid and (refresh or cid not in id_to_dirnode):
            if app in ("", "web", "desktop", "harmony"):
                resp = yield client.fs_files({"cid": cid, "limit": 1}, async_=async_, **request_kwargs)
            else:
                resp = yield client.fs_files_app({"cid": cid, "hide_data": 1}, async_=async_, **request_kwargs)
            check_response(resp)
            if cid and int(resp["path"][-1]["cid"]) != cid:
                raise FileNotFoundError(ENOENT, cid)
            parts.extend(info["name"] for info in resp["path"][1:])
            for info in resp["path"][1:]:
                id_to_dirnode[int(info["cid"])] = DirNode(info["name"], int(info["pid"]))
        else:
            while cid and (not root_id or cid != root_id):
                name, cid = id_to_dirnode[cid]
                parts.append(name)
            parts.reverse()
        if root_id is not None and cid != root_id:
            return ""
        if escape is None:
            path = "/".join(parts)
        else:
            path = "/".join(map(escape, parts))
        if root_id is None or root_id:
            return "/" + path
        else:
            return path
    return run_gen_step(gen_step, async_=async_)


@overload
def get_ancestors_to_cid(
    client: str | P115Client, 
    cid: int = 0, 
    refresh: bool = False, 
    id_to_dirnode: None | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> list[dict]:
    ...
@overload
def get_ancestors_to_cid(
    client: str | P115Client, 
    cid: int = 0, 
    refresh: bool = False, 
    id_to_dirnode: None | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, list[dict]]:
    ...
def get_ancestors_to_cid(
    client: str | P115Client, 
    cid: int = 0, 
    refresh: bool = False, 
    id_to_dirnode: None | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> list[dict] | Coroutine[Any, Any, list[dict]]:
    """获取目录对应的 ancestors（祖先信息列表）

    :param client: 115 客户端或 cookies
    :param cid: 目录的 id
    :param refresh: 是否刷新。如果为 True，则会执行网络请求以查询；如果为 False，则直接从 `id_to_dirnode` 中获取
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典
    :param app: 使用某个 app （设备）的接口
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 目录所对应的祖先信息列表，每一条的结构如下

        .. code:: python

            {
                "id": int, # 目录的 id
                "parent_id": int, # 上级目录的 id
                "name": str, # 名字
            }
    """
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    if id_to_dirnode is None:
        id_to_dirnode = ID_TO_DIRNODE_CACHE[client.user_id]
    def gen_step():
        nonlocal cid
        parts: list[dict] = []
        if cid and (refresh or cid not in id_to_dirnode):
            if app in ("", "web", "desktop", "harmony"):
                resp = yield client.fs_files({"cid": cid, "limit": 1}, async_=async_, **request_kwargs)
            else:
                resp = yield client.fs_files_app({"cid": cid, "hide_data": 1}, async_=async_, **request_kwargs)
            check_response(resp)
            if cid and int(resp["path"][-1]["cid"]) != cid:
                raise FileNotFoundError(ENOENT, cid)
            parts.append({"id": 0, "name": "", "parent_id": 0})
            for info in resp["path"][1:]:
                id, pid, name = int(info["cid"]), int(info["pid"]), info["name"]
                id_to_dirnode[id] = DirNode(name, pid)
                parts.append({"id": id, "name": name, "parent_id": pid})
        else:
            while cid:
                id = cid
                name, cid = id_to_dirnode[cid]
                parts.append({"id": id, "name": name, "parent_id": cid})
            parts.append({"id": 0, "name": "", "parent_id": 0})
            parts.reverse()
        return parts
    return run_gen_step(gen_step, async_=async_)


class P115ID(P115DictAttrLike, int):

    def __str__(self, /) -> str:
        return int.__repr__(self)


# TODO: 使用 search 接口以在特定目录之下搜索某个名字，以便减少风控
@overload
def get_id_to_path(
    client: str | P115Client, 
    path: str | Sequence[str], 
    ensure_file: None | bool = None, 
    is_posixpath: bool = False, 
    refresh: bool = False, 
    id_to_dirnode: None | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> int:
    ...
@overload
def get_id_to_path(
    client: str | P115Client, 
    path: str | Sequence[str], 
    ensure_file: None | bool = None, 
    is_posixpath: bool = False, 
    refresh: bool = False, 
    id_to_dirnode: None | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, int]:
    ...
def get_id_to_path(
    client: str | P115Client, 
    path: str | Sequence[str], 
    ensure_file: None | bool = None, 
    is_posixpath: bool = False, 
    refresh: bool = False, 
    id_to_dirnode: None | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> int | Coroutine[Any, Any, int]:
    """获取路径对应的 id

    :param client: 115 客户端或 cookies
    :param path: 路径
    :param ensure_file: 是否确保为文件

        - True: 必须是文件
        - False: 必须是目录
        - None: 可以是目录或文件

    :param is_posixpath: 使用 posixpath，会把 "/" 转换为 "|"，因此解析的时候，会对 "|" 进行特别处理
    :param refresh: 是否刷新。如果为 True，则会执行网络请求以查询；如果为 False，则直接从 `id_to_dirnode` 中获取
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典
    :param app: 使用某个 app （设备）的接口
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 文件或目录的 id
    """
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    if id_to_dirnode is None:
        id_to_dirnode = ID_TO_DIRNODE_CACHE[client.user_id]
    error = FileNotFoundError(ENOENT, f"no such path: {path!r}")
    def gen_step():
        nonlocal client, ensure_file
        if not isinstance(path, str):
            patht = ["", *filter(None, path)]
            if len(patht) == 1:
                return 0
            if is_posixpath:
                for i in range(1, len(patht)):
                    patht[i] = patht[i].replace("/", "|")
        elif path in (".", "..", "/"):
            if ensure_file:
                raise error
            return 0
        elif path.startswith("根目录 > "):
            patht = path.split(" > ")
            patht[0] = ""
            if is_posixpath:
                for i in range(1, len(patht)):
                    patht[i] = patht[i].replace("/", "|")
        elif is_posixpath:
            if ensure_file is None and path.endswith("/"):
                ensure_file = False
            patht = ["", *filter(None, path.split("/"))]
        else:
            if ensure_file is None and path_is_dir_form(path):
                ensure_file = False
            patht, _ = splits("/" + path)
        if len(patht) == 1:
            if ensure_file:
                raise error
            return 0
        stop = len(patht) - bool(ensure_file)
        obj = "|" if is_posixpath else "/"
        for i in range(stop):
            if obj in patht[i]:
                break
        else:
            i += 1
        j = 1
        pid = 0
        if stop > 1 and not refresh and id_to_dirnode:
            if stop == 2:
                if is_posixpath:
                    needle = (patht[1].replace("/", "|"), pid)
                else:
                    needle = (patht[1], pid)
                for k, t in id_to_dirnode.items():
                    if is_posixpath:
                        t = (t[0].replace("/", "|"), t[1])
                    if t == needle:
                        pid = k
                        j = 2
            else:
                if is_posixpath:
                    table = {(n.replace("/", "|"), pid): k for k, (n, pid) in id_to_dirnode.items()}
                else:
                    table = {cast(tuple[str, int], tuple(t)): k for k, t in id_to_dirnode.items()}
                try:
                    for j in range(1, stop):
                        if is_posixpath:
                            needle = (patht[j].replace("/", "|"), pid)
                        else:
                            needle = (patht[j], pid)
                        pid = table[needle]
                    j += 1
                except KeyError:
                    pass
        if j >= i:
            i = j
            cid = pid
        else:
            if ensure_file and len(patht) == i:
                i -= 1
            if app in ("", "web", "desktop", "harmony"):
                fs_dir_getid: Callable = client.fs_dir_getid
            else:
                fs_dir_getid = partial(client.fs_dir_getid_app, app=app)
            cid = 0
            while i > 1:
                dirname = "/".join(patht[:i])
                resp = yield fs_dir_getid(dirname, async_=async_, **request_kwargs)
                if not (resp["state"] and (cid := resp["id"])):
                    if len(patht) == i and ensure_file is None:
                        ensure_file = True
                        i -= 1
                        continue
                    raise error
                cid = int(cid)
                if not refresh and cid not in id_to_dirnode:
                    yield get_path_to_cid(
                        client, 
                        cid, 
                        id_to_dirnode=id_to_dirnode, 
                        app=app, 
                        async_=async_, 
                        **request_kwargs, 
                    )
                break
        if len(patht) == i:
            return cid
        for name in patht[i:-1]:
            if async_:
                async def request():
                    nonlocal cid
                    async for info in iterdir_raw(
                        client, 
                        cid, 
                        ensure_file=False, 
                        app=app, 
                        id_to_dirnode=id_to_dirnode, 
                        async_=True, 
                        **request_kwargs, 
                    ):
                        attr = _overview_attr(info)
                        if (attr.name.replace("/", "|") if is_posixpath else attr.name) == name:
                            cid = attr.id
                            break
                    else:
                        raise error
                yield request
            else:
                for info in iterdir_raw(
                    client, 
                    cid, 
                    ensure_file=False, 
                    app=app, 
                    id_to_dirnode=id_to_dirnode, 
                    **request_kwargs, 
                ):
                    attr = _overview_attr(info)
                    if (attr.name.replace("/", "|") if is_posixpath else attr.name) == name:
                        cid = attr.id
                        break
                else:
                    raise error
        name = patht[-1]
        if async_:
            async def request():
                async for info in iterdir_raw(
                    client, 
                    cid, 
                    app=app, 
                    id_to_dirnode=id_to_dirnode, 
                    async_=True, 
                    **request_kwargs, 
                ):
                    attr = _overview_attr(info)
                    if (attr.name.replace("/", "|") if is_posixpath else attr.name) == name:
                        if ensure_file:
                            if not attr.is_dir:
                                return P115ID(attr.id, info, about="path")
                        elif attr.is_dir:
                            return P115ID(attr.id, info, about="path")
                else:
                    raise error
            return (yield request)
        else:
            for info in iterdir_raw(
                client, 
                cid, 
                app=app, 
                id_to_dirnode=id_to_dirnode, 
                **request_kwargs, 
            ):
                attr = _overview_attr(info)
                if (attr.name.replace("/", "|") if is_posixpath else attr.name) == name:
                    if ensure_file:
                        if not attr.is_dir:
                            return P115ID(attr.id, info, about="path")
                    elif attr.is_dir:
                        return P115ID(attr.id, info, about="path")
            else:
                raise error
    return run_gen_step(gen_step, async_=async_)


@overload
def get_id_to_pickcode(
    client: str | P115Client, 
    pickcode: str, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> P115ID:
    ...
@overload
def get_id_to_pickcode(
    client: str | P115Client, 
    pickcode: str, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, P115ID]:
    ...
def get_id_to_pickcode(
    client: str | P115Client, 
    pickcode: str, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> P115ID | Coroutine[Any, Any, P115ID]:
    if not 17 <= len(pickcode) <= 18 or not pickcode.isalnum():
        raise ValueError(f"bad pickcode: {pickcode!r}")
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    def gen_step():
        resp = yield client.download_url_web(pickcode, base_url=True, async_=async_, **request_kwargs)
        if file_id := resp.get("file_id"):
            msg_code = resp.get("msg_code", False)
            resp["is_dir"] = msg_code and msg_code != 50028
            return P115ID(file_id, resp, about="pickcode")
        check_response(resp)
    return run_gen_step(gen_step, async_=async_)


@overload
def get_id_to_sha1(
    client: str | P115Client, 
    sha1: str, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> P115ID:
    ...
@overload
def get_id_to_sha1(
    client: str | P115Client, 
    sha1: str, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, P115ID]:
    ...
def get_id_to_sha1(
    client: str | P115Client, 
    sha1: str, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> P115ID | Coroutine[Any, Any, P115ID]:
    if len(sha1) != 40 or sha1.strip(hexdigits):
        raise ValueError(f"bad sha1: {sha1!r}")
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    def gen_step():
        resp = yield client.fs_shasearch(sha1, base_url=True, async_=async_, **request_kwargs)
        check_response(resp)
        resp["data"]["file_sha1"] = sha1.upper()
        return P115ID(resp["data"]["file_id"], resp["data"], about="sha1")
    return run_gen_step(gen_step, async_=async_)


@overload
def filter_na_ids(
    client: str | P115Client, 
    ids: Iterable[int | str], 
    batch_size: int = 50_000, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[int]:
    ...
@overload
def filter_na_ids(
    client: str | P115Client, 
    ids: Iterable[int | str], 
    batch_size: int = 50_000, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[int]:
    ...
def filter_na_ids(
    client: str | P115Client, 
    ids: Iterable[int | str], 
    batch_size: int = 50_000, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[int] | AsyncIterator[int]:
    """找出一组 id 中无效的，所谓无效就是指不在网盘中，可能已经被删除，也可能从未存在过

    :param client: 115 客户端或 cookies
    :param ids: 一组文件或目录的 id
    :param batch_size: 批次大小，分批次，每次提交的 id 数
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，筛选出所有无效的 id
    """
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    file_skim = client.fs_file_skim
    def gen_step():
        if isinstance(ids, Sequence):
            it: Iterator[Iterable[int | str]] = (ids[i:i+batch_size] for i in range(0, len(ids), batch_size))
        else:
            ids_it = iter(ids)
            it = takewhile(bool, (tuple(islice(ids_it, batch_size)) for _ in count()))
        for batch in it:
            resp = yield file_skim(batch, method="POST", async_=async_, **request_kwargs)
            if resp.get("error") == "文件不存在":
                yield YieldFrom(map(int, batch), identity=True)
            else:
                check_response(resp)
                yield YieldFrom(
                    set(map(int, batch)) - {int(a["file_id"]) for a in resp["data"]}, 
                    identity=True, 
                )
    return run_gen_step_iter(gen_step, async_=async_)


@overload
def _iter_fs_files(
    client: str | P115Client, 
    payload: int | str | dict = 0, 
    first_page_size: int = 0, 
    page_size: int = 0, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    ensure_file: None | bool = None, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def _iter_fs_files(
    client: str | P115Client, 
    payload: int | str | dict = 0, 
    first_page_size: int = 0, 
    page_size: int = 0, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    ensure_file: None | bool = None, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def _iter_fs_files(
    client: str | P115Client, 
    payload: int | str | dict = 0, 
    first_page_size: int = 0, 
    page_size: int = 0, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    ensure_file: None | bool = None, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """迭代目录，获取文件信息

    :param client: 115 客户端或 cookies
    :param payload: 请求参数，如果是 int 或 str，则视为 cid
    :param first_page_size: 首次拉取的分页大小
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典
    :param raise_for_changed_count: 分批拉取时，发现总数发生变化后，是否报错
    :param ensure_file: 是否确保为文件

        - True: 必须是文件
        - False: 必须是目录
        - None: 可以是目录或文件

    :param app: 使用某个 app （设备）的接口
    :param cooldown: 冷却时间，大于 0，则使用此时间间隔执行并发
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，返回此目录内的文件信息（文件和目录）
    """
    if isinstance(payload, (int, str)):
        payload = {"cid": payload}
    show_files = payload.get("suffix") or payload.get("type")
    if show_files:
        payload.setdefault("show_dir", 0)
    if ensure_file:
        payload["show_dir"] = 0
        if not show_files:
            payload.setdefault("cur", 1)
    elif ensure_file is False:
        payload["count_folders"] = 1
        payload["fc_mix"] = 0
        payload["show_dir"] = 1
    if payload.get("type") == 99:
        payload.pop("type", None)
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    if id_to_dirnode is None:
        id_to_dirnode = ID_TO_DIRNODE_CACHE[client.user_id]
    def gen_step():
        request_kwargs.update(
            app=app, 
            page_size=page_size, 
            raise_for_changed_count=raise_for_changed_count, 
        )
        if cooldown <= 0:
            it = iter_fs_files(
                client, 
                payload, 
                first_page_size=first_page_size, 
                async_=async_, 
                **request_kwargs, 
            )
        elif async_:
            it = iter_fs_files_asynchronized(
                client, 
                payload, 
                cooldown=cooldown, 
                **request_kwargs, 
            )
        else:
            it = iter_fs_files_threaded(
                client, 
                payload, 
                cooldown=cooldown, 
                **request_kwargs, 
            )
        do_next = anext if async_ else next
        try:
            while True:
                resp = yield do_next(it) # type: ignore
                if id_to_dirnode is not ...:
                    for info in resp["path"][1:]:
                        pid, name = int(info["cid"]), info["name"]
                        id_to_dirnode[pid] = DirNode(name, int(info["pid"]))
                for info in resp["data"]:
                    attr = _overview_attr(info)
                    if attr.is_dir:
                        if id_to_dirnode is not ...:
                            id_to_dirnode[attr.id] = DirNode(attr.name, attr.parent_id)
                    elif ensure_file is False:
                        return
                    yield Yield(info, identity=True)
        except (StopAsyncIteration, StopIteration):
            pass
    return run_gen_step_iter(gen_step, async_=async_)


@overload
def iter_stared_dirs_raw(
    client: str | P115Client, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def iter_stared_dirs_raw(
    client: str | P115Client, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def iter_stared_dirs_raw(
    client: str | P115Client, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """遍历以迭代获得所有被打上星标的目录信息

    :param client: 115 客户端或 cookies
    :param page_size: 分页大小
    :param first_page_size: 首次拉取的分页大小
    :param order: 排序

        - "file_name": 文件名
        - "file_size": 文件大小
        - "file_type": 文件种类
        - "user_utime": 修改时间
        - "user_ptime": 创建时间
        - "user_otime": 上一次打开时间

    :param asc: 升序排列。0: 否，1: 是
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典
    :param raise_for_changed_count: 分批拉取时，发现总数发生变化后，是否报错
    :param app: 使用某个 app （设备）的接口
    :param cooldown: 冷却时间，大于 0，则使用此时间间隔执行并发
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，被打上星标的目录信息
    """
    return _iter_fs_files(
        client, 
        payload={
            "asc": asc, "cid": 0, "count_folders": 1, "cur": 0, "fc_mix": 0, 
            "o": order, "offset": 0, "show_dir": 1, "star": 1, 
        }, 
        page_size=page_size, 
        first_page_size=first_page_size, 
        id_to_dirnode=id_to_dirnode, 
        raise_for_changed_count=raise_for_changed_count, 
        ensure_file=False, 
        app=app, 
        cooldown=cooldown, 
        async_=async_, 
        **request_kwargs, 
    )


@overload
def iter_stared_dirs(
    client: str | P115Client, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def iter_stared_dirs(
    client: str | P115Client, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def iter_stared_dirs(
    client: str | P115Client, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """遍历以迭代获得所有被打上星标的目录信息

    :param client: 115 客户端或 cookies
    :param page_size: 分页大小
    :param first_page_size: 首次拉取的分页大小
    :param order: 排序

        - "file_name": 文件名
        - "file_size": 文件大小
        - "file_type": 文件种类
        - "user_utime": 修改时间
        - "user_ptime": 创建时间
        - "user_otime": 上一次打开时间

    :param asc: 升序排列。0: 否，1: 是
    :param normalize_attr: 把数据进行转换处理，使之便于阅读
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典
    :param raise_for_changed_count: 分批拉取时，发现总数发生变化后，是否报错
    :param app: 使用某个 app （设备）的接口
    :param cooldown: 冷却时间，大于 0，则使用此时间间隔执行并发
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，被打上星标的目录信息
    """
    do_map = lambda f, it: it if not callable(f) else (async_map if async_ else map)(f, it)
    return do_map(normalize_attr, iter_stared_dirs_raw( # type: ignore
        client, 
        page_size=page_size, 
        first_page_size=first_page_size, 
        order=order, 
        asc=asc, 
        id_to_dirnode=id_to_dirnode, 
        raise_for_changed_count=raise_for_changed_count, 
        app=app, 
        cooldown=cooldown, 
        async_=async_, # type: ignore
        **request_kwargs, 
    ))


@overload
def ensure_attr_path[D: dict](
    client: str | P115Client, 
    attrs: Iterable[D], 
    page_size: int = 0, 
    with_ancestors: bool = False, 
    with_path: bool = True, 
    use_star: bool = False, 
    escape: None | Callable[[str], str] = escape, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    errors: Literal["ignore", "raise", "warn"] = "raise", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Collection[D]:
    ...
@overload
def ensure_attr_path[D: dict](
    client: str | P115Client, 
    attrs: Iterable[D], 
    page_size: int = 0, 
    with_ancestors: bool = False, 
    with_path: bool = True, 
    use_star: bool = False, 
    escape: None | Callable[[str], str] = escape, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    errors: Literal["ignore", "raise", "warn"] = "raise", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, Collection[D]]:
    ...
def ensure_attr_path[D: dict](
    client: str | P115Client, 
    attrs: Iterable[D], 
    page_size: int = 0, 
    with_ancestors: bool = False, 
    with_path: bool = True, 
    use_star: bool = False, 
    escape: None | Callable[[str], str] = escape, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    errors: Literal["ignore", "raise", "warn"] = "raise", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Collection[D] | Coroutine[Any, Any, Collection[D]]:
    """为一组文件信息添加 "path" 或 "ancestors" 字段

    :param client: 115 客户端或 cookies
    :param attrs: 一组文件或目录的信息
    :param page_size: 分页大小
    :param with_ancestors: 文件信息中是否要包含 "ancestors"
    :param with_path: 文件信息中是否要包含 "path"
    :param use_star: 获取目录信息时，是否允许使用星标
    :param escape: 对文件名进行转义的函数。如果为 None，则不处理；否则，这个函数用来对文件名中某些符号进行转义，例如 "/" 等
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典
    :param app: 使用某个 app （设备）的接口
    :param errors: 如何处理错误

        - "ignore": 忽略异常后继续
        - "raise": 抛出异常
        - "warn": 输出警告信息后继续

    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 返回这一组文件信息
    """
    if not isinstance(attrs, Collection):
        attrs = tuple(attrs)
    if not (with_ancestors or with_path):
        return attrs
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    if page_size <= 0:
        page_size = 10_000
    elif page_size < 16:
        page_size = 16
    if id_to_dirnode is None:
        id_to_dirnode = ID_TO_DIRNODE_CACHE[client.user_id]
    elif id_to_dirnode is ...:
        id_to_dirnode = {}
    if with_ancestors:
        id_to_ancestors: dict[int, list[dict]] = {}
        def get_ancestors(id: int, attr: dict | tuple[str, int] | DirNode, /) -> list[dict]:
            if isinstance(attr, (DirNode, tuple)):
                name, pid = attr
            else:
                pid = attr["parent_id"]
                name = attr["name"]
            if pid == 0:
                ancestors = [{"id": 0, "parent_id": 0, "name": ""}]
            else:
                if pid not in id_to_ancestors:
                    id_to_ancestors[pid] = get_ancestors(pid, id_to_dirnode[pid])
                ancestors = [*id_to_ancestors[pid]]
            ancestors.append({"id": id, "parent_id": pid, "name": name})
            return ancestors
    if with_path:
        id_to_path: dict[int, str] = {}
        def get_path(attr: dict | tuple[str, int] | DirNode, /) -> str:
            if isinstance(attr, (DirNode, tuple)):
                name, pid = attr
            else:
                pid = attr["parent_id"]
                name = attr["name"]
            if escape is not None:
                name = escape(name)
            if pid == 0:
                return "/" + name
            elif pid in id_to_path:
                return id_to_path[pid] + name
            else:
                dirname = id_to_path[pid] = get_path(id_to_dirnode[pid]) + "/"
                return dirname + name
    def gen_step():
        pids: set[int] = set()
        add_pid = pids.add
        for attr in attrs:
            if pid := attr["parent_id"]:
                add_pid(pid)
            if attr.get("is_dir", False) or attr.get("is_directory", False):
                id_to_dirnode[attr["id"]] = DirNode(attr["name"], pid)
        find_ids: set[int]
        do_through: Callable = async_through if async_ else through
        while pids:
            if find_ids := pids - id_to_dirnode.keys():
                try:
                    if use_star:
                        yield do_through(iter_selected_nodes_using_star_event(
                            client, 
                            find_ids, 
                            normalize_attr=None, 
                            id_to_dirnode=id_to_dirnode, 
                            app=app, 
                            async_=async_, 
                            **request_kwargs, 
                        ))
                    else:
                        yield do_through(iter_selected_nodes_by_edit(
                            client, 
                            find_ids, 
                            normalize_attr=None, 
                            id_to_dirnode=id_to_dirnode, 
                            app=app, 
                            async_=async_, 
                            **request_kwargs, 
                        ))
                except Exception as e:
                    match errors:
                        case "raise":
                            raise
                        case "warn":
                            warn(f"{type(e).__module__}.{type(e).__qualname__}: {e}", category=P115Warning)
            pids = {ppid for pid in pids if (ppid := id_to_dirnode[pid][1])}
        if with_ancestors:
            for attr in attrs:
                try:
                    attr["ancestors"] = get_ancestors(attr["id"], attr)
                except Exception as e:
                    match errors:
                        case "raise":
                            raise
                        case "warn":
                            warn(f"{type(e).__module__}.{type(e).__qualname__}: {e}", category=P115Warning)
                    attr["ancestors"] = None
        if with_path:
            for attr in attrs:
                try:
                    attr["path"] = get_path(attr)
                except Exception as e:
                    match errors:
                        case "raise":
                            raise
                        case "warn":
                            warn(f"{type(e).__module__}.{type(e).__qualname__}: {e}", category=P115Warning)
                    attr["path"] = ""
        return attrs
    return run_gen_step(gen_step, async_=async_)


@overload
def ensure_attr_path_by_category_get[D: dict](
    client: str | P115Client, 
    attrs: Iterable[D], 
    with_ancestors: bool = False, 
    with_path: bool = True, 
    escape: None | Callable[[str], str] = escape, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    max_workers: None | int = 20, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[D]:
    ...
@overload
def ensure_attr_path_by_category_get[D: dict](
    client: str | P115Client, 
    attrs: Iterable[D], 
    with_ancestors: bool = False, 
    with_path: bool = True, 
    escape: None | Callable[[str], str] = escape, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    max_workers: None | int = 20, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[D]:
    ...
def ensure_attr_path_by_category_get[D: dict](
    client: str | P115Client, 
    attrs: Iterable[D], 
    with_ancestors: bool = False, 
    with_path: bool = True, 
    escape: None | Callable[[str], str] = escape, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    max_workers: None | int = 20, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[D] | AsyncIterator[D]:
    """为一组文件信息添加 "path" 或 "ancestors" 字段

    :param client: 115 客户端或 cookies
    :param attrs: 一组文件或目录的信息
    :param with_ancestors: 文件信息中是否要包含 "ancestors"
    :param with_path: 文件信息中是否要包含 "path"
    :param escape: 对文件名进行转义的函数。如果为 None，则不处理；否则，这个函数用来对文件名中某些符号进行转义，例如 "/" 等
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典，如果为 ...，则忽略
    :param app: 使用某个 app （设备）的接口
    :param max_workers: 最大并发数
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器
    """
    if not (with_ancestors or with_path):
        if async_:
            return ensure_aiter(attrs)
        return attrs # type: ignore
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    if id_to_dirnode is None:
        id_to_dirnode = ID_TO_DIRNODE_CACHE[client.user_id]
    elif id_to_dirnode is ...:
        id_to_dirnode = {}
    if app in ("", "web", "desktop", "harmony"):
        func: Callable = partial(client.fs_category_get, **request_kwargs)
    else:
        func = partial(client.fs_category_get_app, app=app, **request_kwargs)
    if with_ancestors:
        id_to_node: dict[int, dict] = {0: {"id": 0, "parent_id": 0, "name": ""}}
        def get_ancestors(id: int, attr: dict | tuple[str, int] | DirNode, /) -> list[dict]:
            if isinstance(attr, (DirNode, tuple)):
                name, pid = attr
                if pid in id_to_node:
                    me = id_to_node[pid] = {"id": id, "parent_id": pid, "name": name}
                else:
                    me = id_to_node[pid]
            else:
                pid = attr["parent_id"]
                name = attr["name"]
                me = {"id": id, "parent_id": pid, "name": name}
            if pid == 0:
                ancestors = [id_to_node[0]]
            else:
                ancestors = get_ancestors(pid, id_to_dirnode[pid])
            ancestors.append(me)
            return ancestors
    if with_path:
        id_to_path: dict[int, str] = {}
        def get_path(attr: dict | tuple[str, int] | DirNode, /) -> str:
            if isinstance(attr, (DirNode, tuple)):
                name, pid = attr
            else:
                pid = attr["parent_id"]
                name = attr["name"]
            if escape is not None:
                name = escape(name)
            if pid == 0:
                return "/" + name
            elif pid in id_to_path:
                return id_to_path[pid] + name
            else:
                dirname = id_to_path[pid] = get_path(id_to_dirnode[pid]) + "/"
                return dirname + name
    waiting: WeakValueDictionary[int, Any] = WeakValueDictionary()
    none: set[int] = set()
    if async_:
        async def async_project(attr: D, /) -> D:
            id = attr["id"]
            pid = attr["parent_id"]
            if pid and pid not in id_to_dirnode:
                async with waiting.setdefault(pid, AsyncLock()):
                    if pid in none:
                        return attr
                    if pid not in id_to_dirnode:
                        resp = await func(id, async_=True)
                        if not resp:
                            none.add(pid)
                            return attr
                        check_response(resp)
                        pid = 0
                        for info in resp["paths"][1:]:
                            fid = int(info["file_id"])
                            id_to_dirnode[fid] = DirNode(info["file_name"], pid)
                            pid = fid
                        if not resp["sha1"]:
                            id_to_dirnode[id] = DirNode(resp["file_name"], pid)
            if with_ancestors:
                attr["ancestors"] = get_ancestors(id, attr)
            if with_path:
                attr["path"] = get_path(attr)
            return attr
        return taskgroup_map(async_project, attrs, max_workers=max_workers)
    else:
        def project(attr: D, /) -> D:
            id = attr["id"]
            pid = attr["parent_id"]
            if pid and pid not in id_to_dirnode:
                with waiting.setdefault(pid, Lock()):
                    if pid in none:
                        return attr
                    if pid not in id_to_dirnode:
                        resp = func(id)
                        if not resp:
                            none.add(pid)
                            return attr
                        check_response(resp)
                        pid = 0
                        for info in resp["paths"][1:]:
                            fid = int(info["file_id"])
                            id_to_dirnode[fid] = DirNode(info["file_name"], pid)
                            pid = fid
                        if not resp["sha1"]:
                            id_to_dirnode[id] = DirNode(resp["file_name"], pid)
            if with_ancestors:
                attr["ancestors"] = get_ancestors(id, attr)
            if with_path:
                attr["path"] = get_path(attr)
            return attr
        return threadpool_map(project, attrs, max_workers=max_workers)


@overload
def iterdir_raw(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    show_dir: Literal[0, 1] = 1, 
    fc_mix: Literal[0, 1] = 1, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    ensure_file: None | bool = None, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def iterdir_raw(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    show_dir: Literal[0, 1] = 1, 
    fc_mix: Literal[0, 1] = 1, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    ensure_file: None | bool = None, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def iterdir_raw(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    show_dir: Literal[0, 1] = 1, 
    fc_mix: Literal[0, 1] = 1, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    ensure_file: None | bool = None, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """迭代目录，获取文件信息

    :param client: 115 客户端或 cookies
    :param cid: 目录 id
    :param page_size: 分页大小
    :param first_page_size: 首次拉取的分页大小
    :param order: 排序

        - "file_name": 文件名
        - "file_size": 文件大小
        - "file_type": 文件种类
        - "user_utime": 修改时间
        - "user_ptime": 创建时间
        - "user_otime": 上一次打开时间

    :param asc: 升序排列。0: 否，1: 是
    :param show_dir: 展示文件夹。0: 否，1: 是
    :param fc_mix: 文件夹置顶。0: 文件夹在文件之前，1: 文件和文件夹混合并按指定排序
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典
    :param raise_for_changed_count: 分批拉取时，发现总数发生变化后，是否报错
    :param ensure_file: 是否确保为文件

        - True: 必须是文件
        - False: 必须是目录
        - None: 可以是目录或文件

    :param app: 使用某个 app （设备）的接口
    :param cooldown: 冷却时间，大于 0，则使用此时间间隔执行并发
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，返回此目录内的文件信息（文件和目录）
    """
    return _iter_fs_files(
        client, 
        payload={
            "asc": asc, "cid": cid, "cur": 1, "count_folders": 1, "fc_mix": fc_mix, 
            "show_dir": show_dir, "o": order, "offset": 0, 
        }, 
        page_size=page_size, 
        first_page_size=first_page_size, 
        id_to_dirnode=id_to_dirnode, 
        raise_for_changed_count=raise_for_changed_count, 
        ensure_file=ensure_file, 
        app=app, 
        cooldown=cooldown, 
        async_=async_, # type: ignore
        **request_kwargs, 
    )


@overload
def iterdir(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    show_dir: Literal[0, 1] = 1, 
    fc_mix: Literal[0, 1] = 1, 
    with_ancestors: bool = False, 
    with_path: bool = False, 
    escape: None | Callable[[str], str] = escape, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    ensure_file: None | bool = None, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def iterdir(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    show_dir: Literal[0, 1] = 1, 
    fc_mix: Literal[0, 1] = 1, 
    with_ancestors: bool = False, 
    with_path: bool = False, 
    escape: None | Callable[[str], str] = escape, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    ensure_file: None | bool = None, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def iterdir(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    show_dir: Literal[0, 1] = 1, 
    fc_mix: Literal[0, 1] = 1, 
    with_ancestors: bool = False, 
    with_path: bool = False, 
    escape: None | Callable[[str], str] = escape, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    ensure_file: None | bool = None, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """迭代目录，获取文件信息

    :param client: 115 客户端或 cookies
    :param cid: 目录 id
    :param page_size: 分页大小
    :param first_page_size: 首次拉取的分页大小
    :param order: 排序

        - "file_name": 文件名
        - "file_size": 文件大小
        - "file_type": 文件种类
        - "user_utime": 修改时间
        - "user_ptime": 创建时间
        - "user_otime": 上一次打开时间

    :param asc: 升序排列。0: 否，1: 是
    :param show_dir: 展示文件夹。0: 否，1: 是
    :param fc_mix: 文件夹置顶。0: 文件夹在文件之前，1: 文件和文件夹混合并按指定排序
    :param with_ancestors: 文件信息中是否要包含 "ancestors"
    :param with_path: 文件信息中是否要包含 "path"
    :param escape: 对文件名进行转义的函数。如果为 None，则不处理；否则，这个函数用来对文件名中某些符号进行转义，例如 "/" 等
    :param normalize_attr: 把数据进行转换处理，使之便于阅读
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典
    :param raise_for_changed_count: 分批拉取时，发现总数发生变化后，是否报错
    :param ensure_file: 是否确保为文件

        - True: 必须是文件
        - False: 必须是目录
        - None: 可以是目录或文件

    :param app: 使用某个 app （设备）的接口
    :param cooldown: 冷却时间，大于 0，则使用此时间间隔执行并发
    :param raise_for_changed_count: 分批拉取时，发现总数发生变化后，是否报错
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，返回此目录内的文件信息（文件和目录）
    """
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    if id_to_dirnode is None:
        id_to_dirnode = ID_TO_DIRNODE_CACHE[client.user_id]
    elif id_to_dirnode is ... and (with_ancestors or with_path):
        id_to_dirnode = {}
    def gen_step():
        nonlocal cid
        it = iterdir_raw(
            client, 
            cid=cid, 
            page_size=page_size, 
            first_page_size=first_page_size, 
            order=order, 
            asc=asc, 
            show_dir=show_dir, 
            fc_mix=fc_mix, 
            id_to_dirnode=id_to_dirnode, 
            raise_for_changed_count=raise_for_changed_count, 
            ensure_file=ensure_file, 
            app=app, 
            cooldown=cooldown, 
            async_=async_, # type: ignore
            **request_kwargs, 
        )
        do_map = lambda f, it: it if not callable(f) else (async_map if async_ else map)(f, it)
        dirname = ""
        pancestors: list[dict] = []
        if with_ancestors or with_path:
            def process(info: dict, /) -> dict:
                nonlocal dirname, pancestors, id_to_dirnode
                id_to_dirnode = cast(dict, id_to_dirnode)
                attr = normalize_attr(info)
                if not pancestors:
                    cid = attr["parent_id"]
                    while cid:
                        name, pid = id_to_dirnode[cid]
                        pancestors.append({"id": cid, "parent_id": pid, "name": name})
                        cid = pid
                    pancestors.append({"id": 0, "parent_id": 0, "name": ""})
                    pancestors.reverse()
                if with_ancestors:
                    attr["ancestors"] = [
                        *pancestors, 
                        {"id": attr["id"], "parent_id": attr["parent_id"], "name": attr["name"]}, 
                    ]
                if with_path:
                    if not dirname:
                        if escape is None:
                            dirname = "/".join(info["name"] for info in pancestors) + "/"
                        else:
                            dirname = "/".join(escape(info["name"]) for info in pancestors) + "/"
                    name = attr["name"]
                    if escape is not None:
                        name = escape(name)
                    attr["path"] = dirname + name
                return attr
            yield YieldFrom(do_map(process, it), identity=True) # type: ignore
        else:
            yield YieldFrom(do_map(normalize_attr, it), identity=True) # type: ignore
    return run_gen_step_iter(gen_step, async_=async_)


@overload
def iter_files_raw(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    suffix: str = "", 
    type: Literal[1, 2, 3, 4, 5, 6, 7, 99] = 99, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    cur: Literal[0, 1] = 0, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def iter_files_raw(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    suffix: str = "", 
    type: Literal[1, 2, 3, 4, 5, 6, 7, 99] = 99, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    cur: Literal[0, 1] = 0, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def iter_files_raw(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    suffix: str = "", 
    type: Literal[1, 2, 3, 4, 5, 6, 7, 99] = 99, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    cur: Literal[0, 1] = 0, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """遍历目录树，获取文件信息

    :param client: 115 客户端或 cookies
    :param cid: 目录 id
    :param page_size: 分页大小
    :param first_page_size: 首次拉取的分页大小
    :param suffix: 后缀名（优先级高于 type）
    :param type: 文件类型

        - 1: 文档
        - 2: 图片
        - 3: 音频
        - 4: 视频
        - 5: 压缩包
        - 6: 应用
        - 7: 书籍
        - 99: 仅文件

    :param order: 排序

        - "file_name": 文件名
        - "file_size": 文件大小
        - "file_type": 文件种类
        - "user_utime": 修改时间
        - "user_ptime": 创建时间
        - "user_otime": 上一次打开时间

    :param asc: 升序排列。0: 否，1: 是
    :param cur: 仅当前目录。0: 否（将遍历子目录树上所有叶子节点），1: 是
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典
    :param raise_for_changed_count: 分批拉取时，发现总数发生变化后，是否报错
    :param app: 使用某个 app （设备）的接口
    :param cooldown: 冷却时间，大于 0，则使用此时间间隔执行并发
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，返回此目录内的（仅文件）文件信息
    """
    suffix = suffix.strip(".")
    if not (type or suffix):
        raise ValueError("please set the non-zero value of suffix or type")
    payload: dict = {
        "asc": asc, "cid": cid, "count_folders": 0, "cur": cur, "o": order, 
        "offset": 0, "show_dir": 0, 
    }
    if suffix:
        payload["suffix"] = suffix
    elif type == 99:
        payload["show_dir"] = 0
    else:
        payload["type"] = type
    return _iter_fs_files(
        client, 
        payload=payload, 
        page_size=page_size, 
        first_page_size=first_page_size, 
        id_to_dirnode=id_to_dirnode, 
        raise_for_changed_count=raise_for_changed_count, 
        ensure_file=True, 
        app=app, 
        cooldown=cooldown, 
        async_=async_, 
        **request_kwargs, 
    )


@overload
def iter_files(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    suffix: str = "", 
    type: Literal[1, 2, 3, 4, 5, 6, 7, 99] = 99, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    cur: Literal[0, 1] = 0, 
    with_ancestors: bool = False, 
    with_path: bool = False, 
    use_star: None | bool = False, 
    escape: None | Callable[[str], str] = escape, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def iter_files(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    suffix: str = "", 
    type: Literal[1, 2, 3, 4, 5, 6, 7, 99] = 99, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    cur: Literal[0, 1] = 0, 
    with_ancestors: bool = False, 
    with_path: bool = False, 
    use_star: None | bool = False, 
    escape: None | Callable[[str], str] = escape, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def iter_files(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    first_page_size: int = 0, 
    suffix: str = "", 
    type: Literal[1, 2, 3, 4, 5, 6, 7, 99] = 99, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    cur: Literal[0, 1] = 0, 
    with_ancestors: bool = False, 
    with_path: bool = False, 
    use_star: None | bool = False, 
    escape: None | Callable[[str], str] = escape, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """遍历目录树，获取文件信息

    :param client: 115 客户端或 cookies
    :param cid: 目录 id
    :param page_size: 分页大小
    :param first_page_size: 首次拉取的分页大小
    :param suffix: 后缀名（优先级高于 type）
    :param type: 文件类型

        - 1: 文档
        - 2: 图片
        - 3: 音频
        - 4: 视频
        - 5: 压缩包
        - 6: 应用
        - 7: 书籍
        - 99: 仅文件

    :param order: 排序

        - "file_name": 文件名
        - "file_size": 文件大小
        - "file_type": 文件种类
        - "user_utime": 修改时间
        - "user_ptime": 创建时间
        - "user_otime": 上一次打开时间

    :param asc: 升序排列。0: 否，1: 是
    :param cur: 仅当前目录。0: 否（将遍历子目录树上所有叶子节点），1: 是
    :param with_ancestors: 文件信息中是否要包含 "ancestors"
    :param with_path: 文件信息中是否要包含 "path"
    :param use_star: 获取目录信息时，是否允许使用星标 （如果为 None，则采用流处理，否则采用批处理）
    :param escape: 对文件名进行转义的函数。如果为 None，则不处理；否则，这个函数用来对文件名中某些符号进行转义，例如 "/" 等
    :param normalize_attr: 把数据进行转换处理，使之便于阅读
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典
    :param raise_for_changed_count: 分批拉取时，发现总数发生变化后，是否报错
    :param app: 使用某个 app （设备）的接口
    :param cooldown: 冷却时间，大于 0，则使用此时间间隔执行并发
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，返回此目录内的（仅文件）文件信息
    """
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    if id_to_dirnode is None:
        id_to_dirnode = ID_TO_DIRNODE_CACHE[client.user_id]
    elif id_to_dirnode is ... and (with_ancestors or with_path):
        id_to_dirnode = {}
    if with_ancestors or with_path:
        cache: list[dict] = []
        add_to_cache = cache.append
    if with_ancestors:
        id_to_ancestors: dict[int, list[dict]] = {}
        def get_ancestors(id: int, attr: dict | tuple[str, int] | DirNode, /) -> list[dict]:
            nonlocal id_to_dirnode
            id_to_dirnode = cast(dict, id_to_dirnode)
            if isinstance(attr, (DirNode, tuple)):
                name, pid = attr
            else:
                pid = attr["parent_id"]
                name = attr["name"]
            if pid == 0:
                ancestors = [{"id": 0, "parent_id": 0, "name": ""}]
            else:
                if pid not in id_to_ancestors:
                    id_to_ancestors[pid] = get_ancestors(pid, id_to_dirnode[pid])
                ancestors = [*id_to_ancestors[pid]]
            ancestors.append({"id": id, "parent_id": pid, "name": name})
            return ancestors
    if with_path:
        id_to_path: dict[int, str] = {}
        def get_path(attr: dict | tuple[str, int] | DirNode, /) -> str:
            nonlocal id_to_dirnode
            id_to_dirnode = cast(dict, id_to_dirnode)
            if isinstance(attr, (DirNode, tuple)):
                name, pid = attr
            else:
                pid = attr["parent_id"]
                name = attr["name"]
            if escape is not None:
                name = escape(name)
            if pid == 0:
                return "/" + name
            elif pid in id_to_path:
                return id_to_path[pid] + name
            else:
                dirname = id_to_path[pid] = get_path(id_to_dirnode[pid]) + "/"
                return dirname + name
    def gen_step():
        it = iter_files_raw(
            client, 
            cid=cid, 
            page_size=page_size, 
            first_page_size=first_page_size, 
            suffix=suffix, 
            type=type, 
            order=order, 
            asc=asc, 
            cur=cur, 
            id_to_dirnode=id_to_dirnode, 
            raise_for_changed_count=raise_for_changed_count, 
            app=app, 
            async_=async_, # type: ignore
            **request_kwargs, 
        )
        do_map = lambda f, it: it if not callable(f) else (async_map if async_ else map)(f, it)
        if with_path or with_ancestors:
            if use_star is None:
                return YieldFrom(ensure_attr_path_by_category_get(
                    client, 
                    do_map(normalize_attr, it), # type: ignore
                    with_ancestors=with_ancestors, 
                    with_path=with_path, 
                    escape=escape, 
                    id_to_dirnode=id_to_dirnode, 
                    app=app, 
                    async_=async_, # type: ignore
                    **request_kwargs, 
                ))
            do_filter = async_filter if async_ else filter
            def process(info):
                attr = normalize_attr(info)
                try:
                    if with_ancestors:
                        attr["ancestors"] = get_ancestors(attr["id"], attr)
                    if with_path:
                        attr["path"] = get_path(attr)
                except KeyError:
                    add_to_cache(attr)
                else:
                    return attr
            yield YieldFrom(do_filter(bool, do_map(process, it)), identity=True) # type: ignore
        else:
            yield YieldFrom(do_map(normalize_attr, it), identity=True) # type: ignore
        if cache and (with_ancestors or with_path):
            yield YieldFrom(ensure_attr_path(
                client, 
                cache, 
                page_size=page_size, 
                with_ancestors=with_ancestors, 
                with_path=with_path, 
                use_star=use_star, # type: ignore
                escape=escape, 
                id_to_dirnode=id_to_dirnode, 
                app=app, 
                async_=async_, # type: ignore
                **request_kwargs, 
            ))
    return run_gen_step_iter(gen_step, async_=async_)


@overload
def traverse_files(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    suffix: str = "", 
    type: Literal[1, 2, 3, 4, 5, 6, 7, 99] = 99, 
    auto_splitting_tasks: bool = True, 
    auto_splitting_threshold: int = 150_000, 
    auto_splitting_statistics_timeout: None | int | float = 5, 
    with_ancestors: bool = False, 
    with_path: bool = False, 
    use_star: None | bool = False, 
    escape: None | Callable[[str], str] = escape, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def traverse_files(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    suffix: str = "", 
    type: Literal[1, 2, 3, 4, 5, 6, 7, 99] = 99, 
    auto_splitting_tasks: bool = True, 
    auto_splitting_threshold: int = 150_000, 
    auto_splitting_statistics_timeout: None | int | float = 5, 
    with_ancestors: bool = False, 
    with_path: bool = False, 
    use_star: None | bool = False, 
    escape: None | Callable[[str], str] = escape, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def traverse_files(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    suffix: str = "", 
    type: Literal[1, 2, 3, 4, 5, 6, 7, 99] = 99, 
    auto_splitting_tasks: bool = True, 
    auto_splitting_threshold: int = 150_000, 
    auto_splitting_statistics_timeout: None | int | float = 5, 
    with_ancestors: bool = False, 
    with_path: bool = False, 
    use_star: None | bool = False, 
    escape: None | Callable[[str], str] = escape, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """遍历目录树，获取文件信息（会根据统计信息，分解任务）

    :param client: 115 客户端或 cookies
    :param cid: 目录 id
    :param page_size: 分页大小
    :param suffix: 后缀名（优先级高于 type）
    :param type: 文件类型

        - 1: 文档
        - 2: 图片
        - 3: 音频
        - 4: 视频
        - 5: 压缩包
        - 6: 应用
        - 7: 书籍
        - 99: 仅文件

    :param auto_splitting_tasks: 是否根据统计信息自动拆分任务
    :param auto_splitting_threshold: 如果 `auto_splitting_tasks` 为 True，且目录内的文件数大于 `auto_splitting_threshold`，则分拆此任务到它的各个直接子目录，否则批量拉取
    :param auto_splitting_statistics_timeout: 如果执行统计超过此时间，则立即终止，并认为文件是无限多
    :param with_ancestors: 文件信息中是否要包含 "ancestors"
    :param with_path: 文件信息中是否要包含 "path"
    :param use_star: 获取目录信息时，是否允许使用星标 （如果为 None，则采用流处理，否则采用批处理）
    :param escape: 对文件名进行转义的函数。如果为 None，则不处理；否则，这个函数用来对文件名中某些符号进行转义，例如 "/" 等
    :param normalize_attr: 把数据进行转换处理，使之便于阅读
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典
    :param app: 使用某个 app （设备）的接口
    :param cooldown: 冷却时间，大于 0，则使用此时间间隔执行并发
    :param raise_for_changed_count: 分批拉取时，发现总数发生变化后，是否报错
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，返回此目录内的（仅文件）文件信息
    """
    from httpx import ReadTimeout

    if not auto_splitting_tasks:
        return iter_files(
            client, 
            cid, 
            page_size=page_size, 
            suffix=suffix, 
            type=type, 
            with_ancestors=with_ancestors, 
            with_path=with_path, 
            use_star=use_star, 
            escape=escape, 
            normalize_attr=normalize_attr, 
            id_to_dirnode=id_to_dirnode, 
            raise_for_changed_count=raise_for_changed_count, 
            app=app, 
            cooldown=cooldown, 
            async_=async_, # type: ignore
            **request_kwargs, 
        )
    suffix = suffix.strip(".")
    if not (type or suffix):
        raise ValueError("please set the non-zero value of suffix or type")
    if suffix:
        suffix = "." + suffix.lower()
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    if page_size <= 0:
        page_size = 10_000
    elif page_size < 16:
        page_size = 16
    if auto_splitting_threshold < 16:
        auto_splitting_threshold = 16
    if id_to_dirnode is None:
        id_to_dirnode = ID_TO_DIRNODE_CACHE[client.user_id]
    elif id_to_dirnode is ... and (with_ancestors or with_path):
        id_to_dirnode = {}
    if app in ("", "web", "desktop", "harmony"):
        fs_files: Callable = client.fs_files
    else:
        fs_files = partial(client.fs_files_app, app=app)
    dq: deque[int] = deque()
    get, put = dq.pop, dq.appendleft
    put(cid)
    def gen_step():
        while dq:
            try:
                if cid := get():
                    # NOTE: 必要时也可以根据不同的扩展名进行分拆任务，通过 client.fs_files_second_type({"cid": cid, "type": type}) 获取目录内所有的此种类型的扩展名，并且如果响应为空时，则直接退出
                    try:
                        payload = {
                            "asc": 1, "cid": cid, "cur": 0, "limit": 16, "o": "user_ptime", "offset": 0, 
                            "show_dir": 0, "suffix": suffix, "type": type, 
                        }
                        resp = check_response((yield fs_files(
                            payload, 
                            async_=async_, 
                            **{
                                **request_kwargs, 
                                "timeout": auto_splitting_statistics_timeout, 
                            }, 
                        )))
                        if cid and int(resp["path"][-1]["cid"]) != cid:
                            continue
                        if id_to_dirnode is not ...:
                            for info in resp["path"][1:]:
                                id_to_dirnode[int(info["cid"])] = DirNode(info["name"], int(info["pid"]))
                    except (ReadTimeout, TimeoutError):
                        file_count = float("inf")
                    else:
                        file_count = int(resp.get("count") or 0)
                    if file_count <= auto_splitting_threshold:
                        if file_count <= 16:
                            attrs = map(normalize_attr, resp["data"])
                            if with_ancestors or with_path:
                                if use_star is None:
                                    attrs = ensure_attr_path_by_category_get( # type: ignore
                                        client, 
                                        attrs, 
                                        with_ancestors=with_ancestors, 
                                        with_path=with_path, 
                                        escape=escape, 
                                        id_to_dirnode=id_to_dirnode, 
                                        app=app, 
                                        async_=async_, 
                                        **request_kwargs, 
                                    )
                                else:
                                    attrs = yield ensure_attr_path(
                                        client, 
                                        attrs, 
                                        page_size=page_size, 
                                        with_ancestors=with_ancestors, 
                                        with_path=with_path, 
                                        use_star=use_star, 
                                        escape=escape, 
                                        id_to_dirnode=id_to_dirnode, 
                                        app=app, 
                                        async_=async_, 
                                        **request_kwargs, 
                                    )
                            yield YieldFrom(attrs, identity=True)
                        else:
                            yield YieldFrom(iter_files(
                                client, 
                                cid, 
                                page_size=page_size, 
                                suffix=suffix, 
                                type=type, 
                                with_ancestors=with_ancestors, 
                                with_path=with_path, 
                                use_star=use_star, 
                                escape=escape, 
                                normalize_attr=normalize_attr, 
                                id_to_dirnode=id_to_dirnode, 
                                raise_for_changed_count=raise_for_changed_count, 
                                app=app, 
                                cooldown=cooldown, 
                                async_=async_, # type: ignore
                                **request_kwargs, 
                            ))
                        continue
                it = iterdir(
                    client, 
                    cid, 
                    page_size=page_size, 
                    with_ancestors=with_ancestors, 
                    with_path=with_path, 
                    escape=escape, 
                    normalize_attr=normalize_attr, 
                    id_to_dirnode=id_to_dirnode, 
                    app=app, 
                    raise_for_changed_count=raise_for_changed_count, 
                    async_=async_, 
                    **request_kwargs, 
                )
                if async_:
                    it = yield to_list(it)
                for attr in cast(Iterable, it):
                    if attr.get("is_dir") or attr.get("is_directory"):
                        put(attr["id"])
                    else:
                        ext = splitext(attr["name"])[1].lower()
                        if suffix:
                            if suffix != ext:
                                continue
                        elif 0 < type <= 7 and type_of_attr(attr) != type:
                            continue
                        yield attr
            except FileNotFoundError:
                pass
    return run_gen_step_iter(gen_step, async_=async_)


@overload
def iter_dupfiles[K](
    client: str | P115Client, 
    cid: int = 0, 
    key: Callable[[dict], K] = itemgetter("sha1", "size"), 
    keep_first: None | bool | Callable[[dict], SupportsLT] = None, 
    page_size: int = 0, 
    suffix: str = "", 
    type: Literal[1, 2, 3, 4, 5, 6, 7, 99] = 99, 
    auto_splitting_tasks: bool = True, 
    auto_splitting_threshold: int = 150_000, 
    auto_splitting_statistics_timeout: None | int | float = 5, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[tuple[K, dict]]:
    ...
@overload
def iter_dupfiles[K](
    client: str | P115Client, 
    cid: int = 0, 
    key: Callable[[dict], K] = itemgetter("sha1", "size"), 
    keep_first: None | bool | Callable[[dict], SupportsLT] = None, 
    page_size: int = 0, 
    suffix: str = "", 
    type: Literal[1, 2, 3, 4, 5, 6, 7, 99] = 99, 
    auto_splitting_tasks: bool = True, 
    auto_splitting_threshold: int = 150_000, 
    auto_splitting_statistics_timeout: None | int | float = 5, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[tuple[K, dict]]:
    ...
def iter_dupfiles[K](
    client: str | P115Client, 
    cid: int = 0, 
    key: Callable[[dict], K] = itemgetter("sha1", "size"), 
    keep_first: None | bool | Callable[[dict], SupportsLT] = None, 
    page_size: int = 0, 
    suffix: str = "", 
    type: Literal[1, 2, 3, 4, 5, 6, 7, 99] = 99, 
    auto_splitting_tasks: bool = True, 
    auto_splitting_threshold: int = 150_000, 
    auto_splitting_statistics_timeout: None | int | float = 5, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    cooldown: int | float = 0, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[tuple[K, dict]] | AsyncIterator[tuple[K, dict]]:
    """遍历以迭代获得所有重复文件

    :param client: 115 客户端或 cookies
    :param cid: 待被遍历的目录 id，默认为根目录
    :param key: 函数，用来给文件分组，当多个文件被分配到同一组时，它们相互之间是重复文件关系
    :param keep_first: 保留某个重复文件不输出，除此以外的重复文件都输出

        - 如果为 None，则输出所有重复文件（不作保留）
        - 如果是 Callable，则保留值最小的那个文件
        - 如果为 True，则保留最早入组的那个文件
        - 如果为 False，则保留最晚入组的那个文件

    :param page_size: 分页大小
    :param suffix: 后缀名（优先级高于 type）
    :param type: 文件类型

        - 1: 文档
        - 2: 图片
        - 3: 音频
        - 4: 视频
        - 5: 压缩包
        - 6: 应用
        - 7: 书籍
        - 99: 仅文件

    :param auto_splitting_tasks: 是否根据统计信息自动拆分任务
    :param auto_splitting_threshold: 如果 `auto_splitting_tasks` 为 True，且目录内的文件数大于 `auto_splitting_threshold`，则分拆此任务到它的各个直接子目录，否则批量拉取
    :param auto_splitting_statistics_timeout: 如果执行统计超过此时间，则立即终止，并认为文件是无限多
    :param normalize_attr: 把数据进行转换处理，使之便于阅读
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典
    :param raise_for_changed_count: 分批拉取时，发现总数发生变化后，是否报错
    :param app: 使用某个 app （设备）的接口
    :param cooldown: 冷却时间，大于 0，则使用此时间间隔执行并发
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，返回 key 和 重复文件信息 的元组
    """
    it: Iterator[dict] | AsyncIterator[dict] = traverse_files(
        client, 
        cid, 
        page_size=page_size, 
        suffix=suffix, 
        type=type, 
        auto_splitting_tasks=auto_splitting_tasks, 
        auto_splitting_threshold=auto_splitting_threshold, 
        auto_splitting_statistics_timeout=auto_splitting_statistics_timeout, 
        normalize_attr=normalize_attr, 
        id_to_dirnode=id_to_dirnode, 
        raise_for_changed_count=raise_for_changed_count, 
        app=app, 
        cooldown=cooldown, 
        async_=async_, # type: ignore
        **request_kwargs, 
    )
    if async_:
        it = cast(AsyncIterator[dict], it)
        return iter_keyed_dups_async(
            it, 
            key=key, 
            keep_first=keep_first, 
        )
    else:
        it = cast(Iterator[dict], it)
        return iter_keyed_dups(
            it, 
            key=key, 
            keep_first=keep_first, 
        )


@overload
def iter_image_files(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 8192, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    cur: Literal[0, 1] = 0, 
    raise_for_changed_count: bool = False, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def iter_image_files(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 8192, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    cur: Literal[0, 1] = 0, 
    raise_for_changed_count: bool = False, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def iter_image_files(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 8192, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    cur: Literal[0, 1] = 0, 
    raise_for_changed_count: bool = False, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """遍历目录树，获取图片文件信息（包含图片的 CDN 链接）

    .. tip::
        这个函数的效果相当于 ``iter_files(client, cid, type=2, ...)`` 所获取的文件列表，只是返回信息有些不同，速度似乎还是 ``iter_files`` 更快

    :param client: 115 客户端或 cookies
    :param cid: 目录 id
    :param page_size: 分页大小
    :param order: 排序

        - "file_name": 文件名
        - "file_size": 文件大小
        - "file_type": 文件种类
        - "user_utime": 修改时间
        - "user_ptime": 创建时间
        - "user_otime": 上一次打开时间

    :param asc: 升序排列。0: 否，1: 是
    :param cur: 仅当前目录。0: 否（将遍历子目录树上所有叶子节点），1: 是
    :param raise_for_changed_count: 分批拉取时，发现总数发生变化后，是否报错
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，返回此目录内的图片文件信息
    """
    def normalize(attr: dict, /):
        for key, val in attr.items():
            if key.endswith(("_id", "_type", "_size", "time")) or key.startswith("is_") or val in "01":
                attr[key] = int(val)
        attr["id"] = attr["file_id"]
        attr["name"] = attr["file_name"]
        return attr
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    if page_size <= 0:
        page_size = 8192
    elif page_size < 16:
        page_size = 16
    payload = {"asc": asc, "cid": cid, "cur": cur, "limit": page_size, "o": order, "offset": 0}
    def gen_step():
        offset = 0
        count = 0
        while True:
            resp = check_response((yield client.fs_imglist_app(payload, async_=async_, **request_kwargs)))
            if int(resp["cid"]) != cid:
                raise FileNotFoundError(ENOENT, cid)
            if count == 0:
                count = int(resp.get("count") or 0)
            elif count != int(resp.get("count") or 0):
                message = f"cid={cid} detected count changes during traversing: {count} => {resp['count']}"
                if raise_for_changed_count:
                    raise P115OSError(EIO, message)
                else:
                    warn(message, category=P115Warning)
                count = int(resp.get("count") or 0)
            if offset != resp["offset"]:
                break
            yield YieldFrom(map(normalize, resp["data"]), identity=True)
            offset += len(resp["data"])
            if offset >= count:
                break
            payload["offset"] = offset
    return run_gen_step_iter(gen_step, async_=async_)


@overload
def iter_dangling_files(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    suffix: str = "", 
    type: Literal[1, 2, 3, 4, 5, 6, 7, 99] = 99, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    app: str = "web", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def iter_dangling_files(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    suffix: str = "", 
    type: Literal[1, 2, 3, 4, 5, 6, 7, 99] = 99, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    app: str = "web", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def iter_dangling_files(
    client: str | P115Client, 
    cid: int = 0, 
    page_size: int = 0, 
    suffix: str = "", 
    type: Literal[1, 2, 3, 4, 5, 6, 7, 99] = 99, 
    normalize_attr: Callable[[dict], dict] = normalize_attr, 
    app: str = "web", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """找出所有悬空的文件，即所在的目录 id 不为 0 且不存在

    .. todo::
        实际上，广义的悬空，包括所有这样的文件或目录，它们的祖先节点中存在一个节点，这个节点的 id 目前不存在于网盘（可能被删除或移入回收站）

    .. danger::
        你可以用 `P115Client.fs_move` 方法，把文件或目录随意移动到任何目录 id 下，即使这个 id 不存在

    .. note::
        你可以用 `P115Client.tool_space` 方法，把所有悬空文件找出来，放到专门的目录中，但这个接口一天只能用一次

    :param client: 115 客户端或 cookies
    :param cid: 目录 id
    :param page_size: 分页大小
    :param suffix: 后缀名（优先级高于 type）
    :param type: 文件类型

        - 1: 文档
        - 2: 图片
        - 3: 音频
        - 4: 视频
        - 5: 压缩包
        - 6: 应用
        - 7: 书籍
        - 99: 仅文件

    :param normalize_attr: 把数据进行转换处理，使之便于阅读
    :param app: 使用某个 app （设备）的接口
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，返回此目录内的（仅文件）文件信息
    """
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    if page_size <= 0:
        page_size = 10_000
    elif page_size < 16:
        page_size = 16
    if app in ("", "web", "desktop", "harmony"):
        fs_files: Callable = client.fs_files
    else:
        fs_files = partial(client.fs_files_app, app=app)
    def gen_step():
        na_cids: set[int] = set()
        ok_cids: set[int] = set()
        payload = {"cid": cid, "limit": page_size, "offset": 0, "suffix": suffix, "type": type}
        while True:
            resp = yield fs_files(payload, async_=async_, **request_kwargs)
            if cid and int(resp["path"][-1]["cid"]) != cid:
                break
            if resp["offset"] != payload["offset"]:
                break
            t = tuple(map(_overview_attr, resp["data"]))
            pids = {
                pid for a in t
                if (pid := a.parent_id) not in na_cids
                    and pid not in ok_cids
            }
            if pids:
                if async_:
                    na_cids.update(filter_na_ids(client, pids, **request_kwargs))
                else:
                    yield async_foreach(
                        na_cids.add, 
                        filter_na_ids(client, pids, async_=True, **request_kwargs), 
                    )
                ok_cids |= pids - na_cids
            for a, info in zip(t, resp["data"]):
                if a.parent_id in na_cids:
                    yield Yield(normalize_attr(info), identity=True)
            payload["offset"] += len(resp["data"]) # type: ignore
            if payload["offset"] >= resp["count"]:
                break
    return run_gen_step_iter(gen_step, async_=async_)


def share_extract_payload(link: str, /) -> SharePayload:
    """从链接中提取 share_code 和 receive_code

    .. hint::
        `link` 支持 3 种形式（圆括号中的字符表示可有可无）：

        1. http(s)://115.com/s/{share_code}?password={receive_code}(#) 或 http(s)://share.115.com/{share_code}?password={receive_code}(#)
        2. (/){share_code}-{receive_code}(/)
        3. {share_code}
    """
    m = CRE_SHARE_LINK_search1(link)
    if m is None:
        m = CRE_SHARE_LINK_search2(link)
    if m is None:
        raise ValueError("not a valid 115 share link")
    return cast(SharePayload, m.groupdict())


@overload
def share_iterdir(
    client: str | P115Client, 
    share_code: str, 
    receive_code: str = "", 
    cid: int = 0, 
    page_size: int = 0, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    normalize_attr: None | Callable[[dict], dict] = normalize_attr, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def share_iterdir(
    client: str | P115Client, 
    share_code: str, 
    receive_code: str = "", 
    cid: int = 0, 
    page_size: int = 0, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    normalize_attr: None | Callable[[dict], dict] = normalize_attr, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def share_iterdir(
    client: str | P115Client, 
    share_code: str, 
    receive_code: str = "", 
    cid: int = 0, 
    page_size: int = 0, 
    order: Literal["file_name", "file_size", "file_type", "user_utime", "user_ptime", "user_otime"] = "user_ptime", 
    asc: Literal[0, 1] = 1, 
    normalize_attr: None | Callable[[dict], dict] = normalize_attr, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """对分享链接迭代目录，获取文件信息

    :param client: 115 客户端或 cookies
    :param share_code: 分享码
    :param receive_code: 接收码
    :param cid: 目录的 id
    :param page_size: 分页大小
    :param order: 排序

        - "file_name": 文件名
        - "file_size": 文件大小
        - "file_type": 文件种类
        - "user_utime": 修改时间
        - "user_ptime": 创建时间
        - "user_otime": 上一次打开时间

    :param asc: 升序排列。0: 否，1: 是
    :param normalize_attr: 把数据进行转换处理，使之便于阅读
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，被打上星标的目录信息
    """
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    if page_size < 0:
        page_size = 10_000
    def gen_step():
        nonlocal receive_code
        if not receive_code:
            resp = yield client.share_info(share_code, async_=async_, **request_kwargs)
            check_response(resp)
            receive_code = resp["data"]["receive_code"]
        payload = {
            "share_code": share_code, 
            "receive_code": receive_code, 
            "cid": cid, 
            "limit": page_size, 
            "offset": 0, 
            "asc": asc, 
            "o": order, 
        }
        count = 0
        while True:
            resp = yield client.share_snap(payload, base_url=True, async_=async_, **request_kwargs)
            check_response(resp)
            if count == (count := resp["data"]["count"]):
                break
            for attr in resp["data"]["list"]:
                attr["share_code"] = share_code
                attr["receive_code"] = receive_code
                if normalize_attr is not None:
                    attr = normalize_attr(attr)
                yield Yield(attr, identity=True)
            payload["offset"] += page_size # type: ignore
            if payload["offset"] >= count: # type: ignore
                break
    return run_gen_step_iter(gen_step, async_=async_)


@overload
def share_iter_files(
    client: str | P115Client, 
    share_link: str, 
    receive_code: str = "", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def share_iter_files(
    client: str | P115Client, 
    share_link: str, 
    receive_code: str = "", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def share_iter_files(
    client: str | P115Client, 
    share_link: str, 
    receive_code: str = "", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """批量获取分享链接中的文件列表

    .. hint::
        `share_link` 支持 3 种形式（圆括号中的字符表示可有可无）：

        1. http(s)://115.com/s/{share_code}?password={receive_code}(#) 或 http(s)://share.115.com/{share_code}?password={receive_code}(#)
        2. (/){share_code}-{receive_code}(/)
        3. {share_code}

        如果使用第 3 种形式，而且又不提供 `receive_code`，则认为这是你自己所做的分享，会尝试自动去获取这个密码

        如果 `share_link` 中有 `receive_code`，而你又单独提供了 `receive_code`，则后者的优先级更高

    :param client: 115 客户端或 cookies
    :param share_link: 分享码或分享链接
    :param receive_code: 密码
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，返回此分享链接下的（仅文件）文件信息，由于接口返回信息有限，所以比较简略

        .. code:: python

            {
                "id": int, 
                "sha1": str, 
                "name": str, 
                "size": int, 
                "path": str, 
            }

    """
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    def gen_step():
        payload: dict = cast(dict, share_extract_payload(share_link))
        if receive_code:
            payload["receive_code"] = receive_code
        elif not payload["receive_code"]:
            resp = yield client.share_info(payload["share_code"], async_=async_, **request_kwargs)
            check_response(resp)
            payload["receive_code"] = resp["data"]["receive_code"]
        payload["cid"] = 0
        it = share_iterdir(client, **payload, async_=async_, **request_kwargs)
        do_next: Callable = anext if async_ else next
        try:
            while True:
                attr = yield do_next(it)
                if attr.get("is_dir") or attr.get("is_directory"):
                    payload["cid"] = attr["id"]
                    resp = yield client.share_downlist(payload, async_=async_, **request_kwargs)
                    check_response(resp)
                    for info in resp["data"]["list"]:
                        fid, sha1 = info["fid"].split("_", 1)
                        yield Yield({
                            "id": int(fid), 
                            "sha1": sha1, 
                            "name": info["fn"], 
                            "size": int(info["si"]), 
                            "path": f"/{info['pt']}/{info['fn']}", 
                        }, identity=True)
                else:
                    yield Yield({k: attr[k] for k in ("id", "sha1", "name", "size", "path")}, identity=True)
        except (StopIteration, StopAsyncIteration):
            pass
    return run_gen_step(gen_step, async_=async_)


@overload
def iter_selected_nodes(
    client: str | P115Client, 
    ids: Iterable[int], 
    normalize_attr: None | Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    max_workers: None | int = 20, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def iter_selected_nodes(
    client: str | P115Client, 
    ids: Iterable[int], 
    normalize_attr: None | Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    max_workers: None | int = 20, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def iter_selected_nodes(
    client: str | P115Client, 
    ids: Iterable[int], 
    normalize_attr: None | Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    max_workers: None | int = 20, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """获取一组 id 的信息

    :param client: 115 客户端或 cookies
    :param ids: 一组文件或目录的 id
    :param normalize_attr: 把数据进行转换处理，使之便于阅读
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典，如果为 ...，则忽略
    :param max_workers: 最大并发数
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，产生详细的信息
    """
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    if id_to_dirnode is None:
        id_to_dirnode = ID_TO_DIRNODE_CACHE[client.user_id]
    def project(resp: dict, /) -> None | dict:
        if resp.get("code") == 20018:
            return None
        check_response(resp)
        info = resp["data"][0]
        if id_to_dirnode is not ...:
            attr = _overview_attr(info)
            if attr.is_dir:
                id_to_dirnode[attr.id] = DirNode(attr.name, attr.parent_id)
        if int(info.get("aid") or info.get("area_id")) != 1:
            return None
        if normalize_attr is None:
            return info
        return normalize_attr(info)
    if async_:
        request_kwargs["async_"] = True
        return async_filter(None, async_map(project, taskgroup_map( # type: ignore
            client.fs_file, ids, max_workers=max_workers, kwargs=request_kwargs)))
    else:
        return filter(None, map(project, threadpool_map(
            client.fs_file, ids, max_workers=max_workers, kwargs=request_kwargs)))


@overload
def iter_selected_nodes_by_category_get(
    client: str | P115Client, 
    ids: Iterable[int], 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    max_workers: None | int = 20, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def iter_selected_nodes_by_category_get(
    client: str | P115Client, 
    ids: Iterable[int], 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    max_workers: None | int = 20, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def iter_selected_nodes_by_category_get(
    client: str | P115Client, 
    ids: Iterable[int], 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    max_workers: None | int = 20, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """获取一组 id 的信息

    :param client: 115 客户端或 cookies
    :param ids: 一组文件或目录的 id
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典，如果为 ...，则忽略
    :param app: 使用某个 app （设备）的接口
    :param max_workers: 最大并发数
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，产生详细的信息
    """
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    if id_to_dirnode is None:
        id_to_dirnode = ID_TO_DIRNODE_CACHE[client.user_id]
    if app in ("", "web", "desktop", "harmony"):
        func: Callable = partial(client.fs_category_get, async_=async_, **request_kwargs)
    else:
        func = partial(client.fs_category_get_app, app=app, async_=async_, **request_kwargs)
    def call(id, /):
        def parse(_, content: bytes):
            resp = loads(content)
            if resp:
                resp["id"] = id
                resp["parent_id"] = int(resp["paths"][-1]["file_id"])
                resp["name"] = resp["file_name"]
                resp["is_dir"] = not resp["sha1"]
            return resp
        return func(id, parse=parse)
    def project(resp: dict, /) -> None | dict:
        if not resp:
            return None
        check_response(resp)
        if id_to_dirnode is not ...:
            pid = 0
            for info in resp["paths"][1:]:
                fid = int(info["file_id"])
                id_to_dirnode[fid] = DirNode(info["file_name"], pid)
                pid = fid
            if resp["is_dir"]:
                id_to_dirnode[resp["id"]] = DirNode(resp["name"], pid)
        return resp
    if async_:
        return async_filter(None, async_map(project, taskgroup_map( # type: ignore
            call, ids, max_workers=max_workers)))
    else:
        return filter(None, map(project, threadpool_map(
            call, ids, max_workers=max_workers)))


@overload
def iter_selected_nodes_by_edit(
    client: str | P115Client, 
    ids: Iterable[int], 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    max_workers: None | int = 20, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def iter_selected_nodes_by_edit(
    client: str | P115Client, 
    ids: Iterable[int], 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    max_workers: None | int = 20, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def iter_selected_nodes_by_edit(
    client: str | P115Client, 
    ids: Iterable[int], 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    max_workers: None | int = 20, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """获取一组 id 的信息

    :param client: 115 客户端或 cookies
    :param ids: 一组文件或目录的 id
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典，如果为 ...，则忽略
    :param max_workers: 最大并发数
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，产生详细的信息
    """
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    if id_to_dirnode is None:
        id_to_dirnode = ID_TO_DIRNODE_CACHE[client.user_id]
    def project(resp: dict, /) -> None | dict:
        if resp.get("error") == "文件不存在/数据库错误了":
            return None
        check_response(resp)
        info = resp["data"]
        info["id"] = int(info["file_id"])
        info["parent_id"] = int(info["parent_id"])
        info["name"] = info["file_name"]
        info["is_dir"] = not info["sha1"]
        if id_to_dirnode is not ... and info["is_dir"]:
            id_to_dirnode[info["id"]] = DirNode(info["name"], info["parent_id"])
        return info
    args_it = ({"file_id": fid, "show_play_long": 1} for fid in ids)
    if async_:
        request_kwargs["async_"] = True
        return async_filter(None, async_map(project, taskgroup_map( # type: ignore
            client.fs_edit_app, args_it, max_workers=max_workers, kwargs=request_kwargs)))
    else:
        return filter(None, map(project, threadpool_map(
            client.fs_edit_app, args_it, max_workers=max_workers, kwargs=request_kwargs)))


@overload
def iter_selected_nodes_using_star_event(
    client: str | P115Client, 
    ids: Iterable[int], 
    with_pics: bool = False, 
    normalize_attr: None | bool | Callable[[dict], dict] = True, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def iter_selected_nodes_using_star_event(
    client: str | P115Client, 
    ids: Iterable[int], 
    with_pics: bool = False, 
    normalize_attr: None | bool | Callable[[dict], dict] = True, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def iter_selected_nodes_using_star_event(
    client: str | P115Client, 
    ids: Iterable[int], 
    with_pics: bool = False, 
    normalize_attr: None | bool | Callable[[dict], dict] = True, 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    app: str = "web", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """通过打星标来获取一组 id 的信息

    :param client: 115 客户端或 cookies
    :param ids: 一组文件或目录的 id
    :param with_pics: 包含图片的 id
    :param normalize_attr: 把数据进行转换处理，使之便于阅读
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典，如果为 ...，则忽略
    :param app: 使用某个 app （设备）的接口
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，产生简略的信息

        .. code:: python

            {
                "id": int, 
                "parent_id": int, 
                "name": str, 
                "is_dir": 0 | 1, 
                "pickcode": str, 
                "sha1": str, 
                "size": int, 
                "star": 0 | 1, 
                "labels": list[dict], 
                "ftype": int, 
                "type": int, 
            }
    """
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    if id_to_dirnode is None:
        id_to_dirnode = ID_TO_DIRNODE_CACHE[client.user_id]
    def gen_step():
        nonlocal ids
        ts = int(time())
        ids = set(ids)
        yield life_show(client, async_=async_, **request_kwargs)
        yield update_star(client, ids, async_=async_, **request_kwargs)
        discard = ids.discard
        it = iter_life_behavior_once(
            client, 
            from_time=ts, 
            type="star_file", 
            app=app, 
            async_=async_, 
            **request_kwargs, 
        )
        if with_pics:
            it2 = iter_life_behavior_once(
                client, 
                from_time=ts, 
                type="star_image_file", 
                app=app, 
                async_=async_, 
                **request_kwargs, 
            )
            if async_:
                it = async_chain(it, it2)
            else:
                it = chain(it, it2) # type: ignore
        do_next = anext if async_ else next
        try:
            while True:
                event: dict = yield do_next(it) # type: ignore
                fid = int(event["file_id"])
                pid = int(event["parent_id"])
                name = event["file_name"]
                is_dir = not event["file_category"]
                if is_dir and id_to_dirnode is not ...:
                    id_to_dirnode[fid] = DirNode(name, pid)
                if fid in ids:
                    if not normalize_attr:
                        yield Yield(event, identity=True)
                    elif normalize_attr is True:
                        attr = {
                            "id": fid, 
                            "parent_id": pid, 
                            "name": name, 
                            "is_dir": is_dir, 
                            "pickcode": event["pick_code"], 
                            "sha1": event["sha1"], 
                            "size": event["file_size"], 
                            "star": event["is_mark"], 
                            "labels": event["fl"], 
                            "ftype": event["file_type"], 
                        }
                        if attr["is_dir"]:
                            attr["type"] = 0
                        elif event.get("isv"):
                            attr["type"] = 4
                        elif event.get("play_long"):
                            attr["type"] = 3
                        else:
                            attr["type"] = type_of_attr(attr)
                        yield Yield(attr, identity=True)
                    else:
                        yield Yield(normalize_attr(event), identity=True)
                    discard(fid)
                    if not ids:
                        break
        except (StopIteration, StopAsyncIteration):
            pass
    return run_gen_step_iter(gen_step, async_=async_)


@overload
def iter_selected_dirs_using_star(
    client: str | P115Client, 
    ids: Iterable[int], 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def iter_selected_dirs_using_star(
    client: str | P115Client, 
    ids: Iterable[int], 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def iter_selected_dirs_using_star(
    client: str | P115Client, 
    ids: Iterable[int], 
    id_to_dirnode: None | EllipsisType | dict[int, tuple[str, int] | DirNode] = None, 
    raise_for_changed_count: bool = False, 
    app: str = "web", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """通过打星标来获取一组 id 的信息（仅支持目录）

    :param client: 115 客户端或 cookies
    :param ids: 一组目录的 id（如果包括文件，则会被忽略）
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``DirNode(name, parent_id)`` 命名元组的字典
    :param raise_for_changed_count: 分批拉取时，发现总数发生变化后，是否报错
    :param app: 使用某个 app （设备）的接口
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，产生详细的信息
    """
    if not isinstance(client, P115Client):
        client = P115Client(client, check_for_relogin=True)
    def gen_step():
        nonlocal ids
        ts = int(time())
        ids = set(ids)
        yield update_star(client, ids, async_=async_, **request_kwargs)
        yield update_desc(client, ids, async_=async_, **request_kwargs)
        discard = ids.discard
        it = iter_stared_dirs(
            client, 
            order="user_utime", 
            asc=0, 
            first_page_size=64, 
            id_to_dirnode=id_to_dirnode, 
            normalize_attr=normalize_attr, 
            app=app, 
            async_=async_, # type: ignore
            **request_kwargs, 
        )
        do_next = anext if async_ else next
        try:
            while True:
                info: dict = yield do_next(it) # type: ignore
                if normalize_attr is None:
                    attr: Any = _overview_attr(info)
                else:
                    attr = info
                if not (attr["mtime"] >= ts and attr["is_dir"]):
                    break
                cid = attr["id"]
                if cid in ids:
                    yield Yield(info, identity=True)
                    discard(cid)
                    if not ids:
                        break
        except (StopIteration, StopAsyncIteration):
            pass
    return run_gen_step_iter(gen_step, async_=async_)

