#新任务：
#确认re_init_with_snapshot函数及实现相关（SECS驱动列表更新）功能
#正在做：
#读SQLalchemy的official manual，确认dbsession的各种用法（current reading task）；
#已确认/实现：
#process_param_data部分zip的使用，svid在SECS driver存入的格式（list），以及排列对stepparameter表匹配录入逻辑的影响
#->已确认逻辑，需要根据SECS driver svid list和param2svid表的mapping顺序决定key和value的layout

import logging
import asyncio
import nats
import json
import os
import click
import abc
import requests
import pprint
import time
import dotenv

from dataclasses import dataclass
from nats.errors import ConnectionClosedError, TimeoutError, NoServersError
from flask import Blueprint
from typing import (
    # List,
    Literal,
    NamedTuple,
    Optional,
    Union,
    Tuple,
    TypedDict,
)
from sqlalchemy import select

from apc.app import create_app
from apc.extension import db
from apc.model.step_param import ParamSVIDMap, StepParameter

SVID = int
CEID = int
RPTID = int
RPID = int

dotenv.load_dotenv()

blp = Blueprint("svid", __name__, url_prefix="svid", cli_group=None)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

EQUIPMENT = os.environ.get("EQUIPMENT", "")
SECS_DRIVER_HOST = os.environ.get("SECS_DRIVER_HOST", "")
SECS_DRIVER_PORT = os.environ.get("SECS_DRIVER_PORT", "")
SECS_KVM_CMD_ENDPOINT = f"http://{SECS_DRIVER_HOST}:{SECS_DRIVER_PORT}/secs/send_kvf"

PM_ALIAS = Literal["PM1", "PM2"]
TM_ALIAS = Literal["TM01", "TM02", "TM03", "TM04", "TM05"]
CHAMBER_NAME = Literal[
    "H01", "H02", "H03", "H04", "H05", "H06", "H07", "H08", "H09", "H10"
]

tool_chamber_alias: dict[CHAMBER_NAME, Tuple[TM_ALIAS, PM_ALIAS]] = {
    "H01": ("TM01", "PM1"),
    "H02": ("TM01", "PM2"),
    "H03": ("TM02", "PM1"),
    "H04": ("TM02", "PM2"),
    "H05": ("TM03", "PM1"),
    "H06": ("TM03", "PM2"),
    "H07": ("TM04", "PM1"),
    "H08": ("TM04", "PM2"),
    "H09": ("TM05", "PM1"),
    "H10": ("TM05", "PM2"),
}


@dataclass(frozen=True)
class StreamFunction:
    Stream: int
    Function: int
    Name: str
    Description: str


S6F11 = StreamFunction(
    6,
    11,
    "Event Report Send (ERS)",
    "The purpose of this message is for the equipment to send a defined, linked and enabled group of reports to the host upon the occurrence of an event (CEID).",
)


class NatsMsgHeader(TypedDict):
    name: str
    port: str
    port_idx: int
    active: bool
    use: int
    system_id: int
    sf_name: str
    format_type: int


class NatsMsgData(TypedDict):
    RPID: int
    V: list[Union[str, float]]


class NatsMsgDataPayload(TypedDict):
    DATAID: int
    CEID: int
    DATA: NatsMsgData


class NatsMessage(TypedDict):
    header: NatsMsgHeader
    data: NatsMsgDataPayload


class NatsEventDataItem(TypedDict):
    RPTID: int
    V: list[Union[str, int]]


class NatsEventData(TypedDict):
    DATAID: int
    CEID: int
    DATA: list[NatsEventDataItem]


class NatsEventValue(TypedDict):
    PortID: int
    SessionID: int
    SystemID: int
    Stream: int
    Function: int
    FmtType: str
    Data: Optional[NatsEventData]


class NatsEventMessage(TypedDict):
    Eqp: str
    Name: str
    Value: NatsEventValue
    Type: int


PM1_RECIPE_STEP_STATISTICS_CEID = 130000027
PM2_RECIPE_STEP_STATISTICS_CEID = 1130000027

PM1_RECIPE_STEP_INFO_RPID = 10001
PM2_RECIPE_STEP_INFO_RPID = 10002

app = create_app()


@blp.cli.command("listen-svid")
def start_listener():
    logger.info(f"START Listening: {EQUIPMENT}!")
    asyncio.run(monitor_param_svid_map())
    logger.info("STOP Listener!")


class ParamSVIDPair(NamedTuple):
    svid: int
    param: str


def get_param_svid_snapshot() -> tuple[ParamSVIDPair, ...]:
    result = db.session.execute(
        select(
            ParamSVIDMap.svid,
            ParamSVIDMap.param_name,
        ).order_by(ParamSVIDMap.id)
    )
    rows = [tuple(row) for row in result]
    return tuple(ParamSVIDPair(*r) for r in rows)


async def monitor_param_svid_map(interval: float = 60.0):
    """
    功能函数，每 30(interval) 秒检查一次 ParamSVIDMap表，
    有任何变更就启动update_and_restart
    """
    
    last_snapshot: tuple[ParamSVIDPair, ...] = tuple()

    while True:
        try:
            snapshot = get_param_svid_snapshot()
            logger.info("尝试读取ParamSVIDMap表")
            # 表中有变化时执行：
            if snapshot != last_snapshot:
                await update_and_restart(snapshot)
                last_snapshot = snapshot  # 设为新checkpoint
                logger.info("ParamSVIDMap已更新，配置初始化")
        except Exception as exc:
            logger.error("param_svid_map monitor error: %s", exc, exc_info=True)
        finally:
            db.session.rollback()
        await asyncio.sleep(interval)


_listener_task: Optional[asyncio.Task] = None  # 正在运行的listener_main task


async def update_and_restart(snapshot: tuple[ParamSVIDPair, ...]):
    """
    monitor_param_svid_map 推来新的 SVID 时调用：
      1) 暂停当前 listener_main监听
      2) 将snapshot传到process_param_data
      3) 重启监听
    """
    global _listener_task

    # 停止正在运行的 listener_main
    if _listener_task and not _listener_task.done():
        _listener_task.cancel()
        try:
            await _listener_task
        except asyncio.CancelledError:
            pass  # 让计划中的listener_main停止不报错

    # 将snapshot发给init_default_from_snapshot,初始化机台
    # re_init_with_snapshot(snapshot)

    # 重启 listener_main，同时发送snapshot
    _listener_task = asyncio.create_task(listener_main(snapshot))
    logger.info("Restarting listener post reinitialisation.")


async def listener_main(snapshot: tuple[ParamSVIDPair, ...]):
    NATS_URL = app.config["NATS_URL"]
    logger.info(f"Connect to NATS: {NATS_URL}")
    nc = await nats.connect(f"{NATS_URL}")
    *_, index = EQUIPMENT.split("TM")
    topic = f"eap.event.TM{int(index)}"
    logger.info(f"Enter Monitor Topic: {topic} Loop")
    sub = await nc.subscribe(topic)
    while True:
        try:
            async for msg in sub.messages:
                message_handler(msg, snapshot)
            await asyncio.sleep(0.5)
            logger.info("Sleep 0.5 sec!")
        except (ConnectionClosedError, TimeoutError, NoServersError):
            logger.error("Nats连接异常, 5秒后重新连接", exc_info=True)
            await asyncio.sleep(5)
            nc = await nats.connect(f"{NATS_URL}")
            sub = await nc.subscribe(topic)
        except KeyboardInterrupt:
            logger.critical("用户主动退出")
            break
        except Exception as exc:
            logger.error(f"未知错误: {str(exc)}", exc_info=True)
            await asyncio.sleep(0.5)


@dataclass(frozen=True)
class EventPayload:
    rptid: int
    chamber: str  # PM1/PM2
    params: list[Union[str, int]]


def message_handler(msg, snapshot: tuple[ParamSVIDPair, ...]):
    data: NatsEventMessage = json.loads(msg.data.decode())
    stream = int(data["Value"]["Stream"])
    function = int(data["Value"]["Function"])

    if stream != S6F11.Stream or function != S6F11.Function:
        return

    logger.info(f"[HIT] S{stream},F{function}")
    try:
        ceid = data["Value"]["Data"]["CEID"]
        rpts = data["Value"]["Data"]["DATA"]
    except (KeyError, TypeError, ValueError):
        logger.debug(f"[Bypass]: {data}")
        return

    if ceid == PM1_RECIPE_STEP_STATISTICS_CEID:
        chamber = "PM1"
        target_rpid = PM1_RECIPE_STEP_INFO_RPID
    elif ceid == PM2_RECIPE_STEP_STATISTICS_CEID:
        chamber = "PM2"
        target_rpid = PM2_RECIPE_STEP_INFO_RPID
    else:
        return

    for rpt in rpts:
        if rpt["RPTID"] != target_rpid:
            continue
        payload = EventPayload(rptid=target_rpid, chamber=chamber, params=rpt["V"])
        process_param_data(snapshot, payload)
        return


def resolve_chamber_name(TM_ALIAS: str, PM_ALIAS: str) -> str:
    """依据监控的机台/TM 与 腔室/PM 组合， 反推腔室编号/H -> chamber_name。"""
    for chamber, (tm, pm) in tool_chamber_alias.items():
        if tm == TM_ALIAS and pm == PM_ALIAS:
            return chamber
    raise ValueError(f"Unknown chamber name combination for {TM_ALIAS}, {PM_ALIAS}")


def process_param_data(snapshot: tuple[ParamSVIDPair, ...], payload: EventPayload):
    """
    将每个工步上抛的paramter数据，写入 StepParameter的表
    """

    # 查询对应的chamber_name
    # 默认一个进程对应一个机台/TM号，用docker.compose多个脚本监控不同机台
    tm_alias = (EQUIPMENT).strip()
    chamber_name = resolve_chamber_name(tm_alias, payload.chamber)

    # 解析 rpt["V"],读取前四个默认字段
    # Need to testout current layout for the default [4:]
    _, step_name_raw, lot_no_raw, _ = payload.params[:4]
    # _, _, step_name_raw, lot_no_raw = payload.params[:4]
    step_name = str(step_name_raw).strip()
    lot_no = str(lot_no_raw).strip()

    # 配对ParamSVIDMap-pm_alias和机台发送的chamber,
    # rpt["V"]第五位以后的值 -> payload.param[4:])为value,
    # trim掉param_name行前四个letter(PM1/|PM2/)
    # 形成{param_name:value}, 录入表中parameters列
    parameters: dict[str, object] = {}
    for row, value in zip(snapshot, payload.params[4:]):
        param_name = row.param
        trimmed = param_name[4:]
        parameters[trimmed] = value

    # 写入 StepParameter 表的值：
    db.session.add(
        StepParameter(
            lot_no=lot_no,  # "JobID value"
            chamber_name=chamber_name,  # From resolve_chamber_name func logic
            step_name=step_name,  # "Recipe.Message_dv value",
            pameters=parameters,  # 录入字段为{param_name: value}
            overflow=None,  # 默认为null, 如有需要写入的值再赋值
        )
    )
    db.session.commit()
    logger.info(
        f"Values in message {payload.rptid}; {payload.chamber} has been written in the step_parameter table."
    )


# ===============================================SECS驱动列表更新(开发中，未实现）=====================================================
@blp.cli.command("pull-svid")
def start_getter():
    start_timestamp = time.time()
    logger.info(
        f"START Getter {EQUIPMENT} http://{SECS_DRIVER_HOST}:{SECS_DRIVER_PORT}"
    )

    while True:
        if time.time() - start_timestamp < GETTER_LOOP_INTERVAL:
            time.sleep(1)
            continue

        try:
            request_annotated_event_report(PM1_RECIPE_STEP_INFO_RPID)
            request_annotated_event_report(PM2_RECIPE_STEP_INFO_RPID)
        except KeyboardInterrupt:
            break
        except Exception:
            logger.error("发生异常", exc_info=True)
        finally:
            start_timestamp = time.time()

    logger.info("STOP Getter!")


def re_init_with_snapshot(snapshot: tuple[ParamSVIDPair, ...]) -> None:
    """
    使用传入的 snapshot 直接下更新SVID 列表到机台
    """
    sv_ids = [int(p.svid) for p in snapshot]

    # PM1
    ceid = PM1_RECIPE_STEP_STATISTICS_CEID
    rptid = PM1_RECIPE_STEP_INFO_RPID
    logger.info("[PM1] re-init")
    define_report_reset(rptid)
    result = define_report(rptid, sv_ids)
    rptids = request_event_report(ceid)
    rptids.extend([rptid])
    result = link_event_report_reset(ceid)
    result = link_event_report(ceid, rptids)
    logger.info(f"{result}")

    # PM2
    ceid = PM2_RECIPE_STEP_STATISTICS_CEID
    rptid = PM2_RECIPE_STEP_INFO_RPID
    logger.info("[PM2] re-init")
    result = define_report_reset(rptid)
    result = define_report(rptid, sv_ids)
    rptids = request_event_report(ceid)
    rptids.extend([rptid])
    result = link_event_report_reset(ceid)
    result = link_event_report(ceid, rptids)
    logger.info(f"{result}")


GETTER_LOOP_INTERVAL = 60

KVFDataFormat = Literal[
    "V",  # "可变格式"
    "L",  # "列表, Value:需要是数组"
    "B",  # "字节"
    "BOOLEAN",  # "bool类型"
    "U1",  # "无符号整数，传入普通int即可, 但不能是负数"
    "U2",  # "无符号整数"
    "U4",  # "无符号整数"
    "U8",  # "无符号整数"
    "I1",  # "有符号整数, 传入普通int即可"
    "I2",  # "有符号整数"
    "I4",  # "有符号整数"
    "I8",  # "有符号整数"
    "F4",  # "小数, 传入普通float"
    "F8",  # "小数"
    "A",  # "字符串，传入字符串或者byte数组"
]


class KvfValue(abc.ABC):
    pass


class KvfU4(KvfValue):
    def __init__(self, value: int, name: str = "") -> None:
        self.value = value
        self.name = name

    def todict(self):
        return dict(Name=self.name, Value=self.value, Format="U4")


class KvfList(KvfValue):
    def __init__(self, value: list[Union[KvfU4, "KvfList"]], name: str = "") -> None:
        self.value = value
        self.name = name

    def todict(self):
        return dict(
            Name=self.name,
            Value=[v.todict() if isinstance(v, KvfValue) else v for v in self.value],
            Format="L",
        )


class KVFDataSchema(TypedDict):
    Name: str
    Format: KVFDataFormat
    Value: Union[
        int, float, str, "KVFDataSchema", list["KVFDataSchema"], list[int], list[str]
    ]


class KVFResponseData(TypedDict):
    Data: KVFDataSchema


class KVFResponse(TypedDict):
    Data: KVFResponseData
    Err: Optional[str]


S2F33 = StreamFunction(
    2,
    33,
    "Define Report (DR)",
    "The purpose of this message is for the host to define a group of reports for the equipment.",
)

S2F35 = StreamFunction(
    2,
    35,
    "Link Event Report (LER)",
    "The purpose of this message is for the host to link n reports to an event (CEID). These linked event reports will default to ‘disabled’ upon linking. That is, the occurrence of an event would not cause the report to be sent until enabled.",
)

S6F21 = StreamFunction(
    6,
    21,
    "Annotated Individual Report Request (AIRR)",
    "The purpose of this message is for the host to request a defined report from the equipment",
)

S6F15 = StreamFunction(
    6,
    15,
    "Event Report Request (ERR)",
    "The purpose of this message is for the host to demand a given report group from the equipment.",
)


def define_report_reset(report_id: int):
    return define_report(report_id, [])


def define_report(report_id: int, svids: list[int]) -> KVFResponse:
    """更新RPID"""
    data = KvfList(
        [
            KvfU4(1, "DATAID"),
            KvfList(
                [KvfList([KvfU4(report_id), KvfList([KvfU4(svid) for svid in svids])])]
            ),
        ]
    )
    req = KVFRequest(
        Name=EQUIPMENT,
        Stream=S2F33.Stream,
        Function=S2F33.Function,
        Data=data.todict(),
    )
    # logger.info(f"bind svids:{svids} to rpid:{report_id}: {pprint.pformat(req)}")
    r = requests.post(
        SECS_KVM_CMD_ENDPOINT,
        json=req,
    )
    r.raise_for_status()
    return r.json()


def request_annotated_event_report(rptid: RPTID) -> list[int]:
    """
    The purpose of this message is for the host to request a defined report from the equipment
    {'Data': {'Data': {'Format': 'L',
                   'Name': '',
                   'Value': [{'Format': 'L',
                              'Name': '',
                              'Value': [{'Format': 'U4',
                                         'Name': '',
                                         'Value': 4},
                                        {'Format': 'A',
                                         'Name': '',
                                         'Value': '2025101617411422'}]},
                             {'Format': 'L',
                              'Name': '',
                              'Value': [{'Format': 'U4',
                                         'Name': '',
                                         'Value': 1110000005},
                                        {'Format': 'F8',
                                         'Name': '',
                                         'Value': 21}]},
                             {'Format': 'L',
                              'Name': '',
                              'Value': [{'Format': 'U4',
                                         'Name': '',
                                         'Value': 1110000009},
                                        {'Format': 'A',
                                         'Name': '',
                                         'Value': 'HT_Baking'}]}]},
          'Function': 22,
          'Stream': 6},
    'Err': None}
    """
    data = KvfU4(rptid)
    req = KVFRequest(
        Name=EQUIPMENT,
        Stream=S6F21.Stream,
        Function=S6F21.Function,
        Data=data.todict(),
    )
    r = requests.post(
        SECS_KVM_CMD_ENDPOINT,
        json=req,
    )
    r.raise_for_status()
    rsp = r.json()
    logger.info(f"\n{pprint.pformat(rsp)}")
    svids = rsp["Data"]["Data"]["Value"]
    result = [int(svid["Value"][0]["Value"]) for svid in svids]
    return result


"""
1. 查询CEID中绑定了那些Report ID
2. 清空CEID中绑定的Report ID
3. 将原来的report id + 新加的report id重新绑定CEID
4. 查询CEID中绑定的Report id (double check)
"""


def request_event_report(ceid: CEID) -> list[RPTID]:
    """
    rsp = {
        "Data": {
            "Data": {
                "Name": "",
                "Format": "L",
                "Value": [
                    {"Name": "", "Format": "U2", "Value": 0},
                    {"Name": "", "Format": "U4", "Value": 1130000036},
                    {
                        "Name": "",
                        "Format": "L",
                        "Value": [
                            {
                                "Name": "",
                                "Format": "L",
                                "Value": [
                                    {"Name": "", "Format": "U4", "Value": 1150000001},
                                    {
                                        "Name": "",
                                        "Format": "L",
                                        "Value": [
                                            {
                                                "Name": "",
                                                "Format": "A",
                                                "Value": "2025101617091491",
                                            },
                                            {"Name": "", "Format": "F8", "Value": 18},
                                            {
                                                "Name": "",
                                                "Format": "A",
                                                "Value": "LT_Baking",
                                            },
                                        ],
                                    },
                                ],
                            }
                        ],
                    },
                ],
            },
            "Function": 16,
            "Stream": 6,
        },
        "Err": None,
    }
    """

    data = KvfU4(ceid)
    req = KVFRequest(
        Name=EQUIPMENT,
        Stream=S6F15.Stream,
        Function=S6F15.Function,
        Data=data.todict(),
    )
    r = requests.post(
        SECS_KVM_CMD_ENDPOINT,
        json=req,
    )
    r.raise_for_status()
    rsp = r.json()
    logger.info(f"\n{pprint.pformat(rsp)}")
    rsp_ceid = int(rsp["Data"]["Data"]["Value"][1]["Value"])
    assert rsp_ceid == ceid
    rpts = rsp["Data"]["Data"]["Value"][2]["Value"] or []
    return [int(rpt["Value"][0]["Value"]) for rpt in rpts]


class KVFRequest(TypedDict):
    Name: str  # string	是	机台名(也是EAP名称)
    # Target: NotRequired[str]  # string	否	发送给机台或PT(可选值client,bridge)
    Stream: int  # int	是	指令Stream
    Function: int  # int	是	指令Function
    Data: dict  # dict	是	{Name:xx,Format:xx,Value:xxx}形式
    # Priority: NotRequired[int]  # int	否	优先级(一般大于100)


def link_event_report_reset(ceid: int) -> KVFResponse:
    """绑定RPID和CEDI"""
    return link_event_report(ceid, [])


def link_event_report(ceid: int, rpids: list[int]) -> KVFResponse:
    """绑定RPID和CEDI"""
    data = KvfList(
        [
            KvfU4(1, "DATAID"),
            KvfList([KvfList([KvfU4(ceid), KvfList([KvfU4(rpid) for rpid in rpids])])]),
        ]
    )
    req = KVFRequest(
        Name=EQUIPMENT,
        Stream=S2F35.Stream,
        Function=S2F35.Function,
        Data=data.todict(),
    )

    # logger.info(f"bind rpids:{rpids} to {ceid}: {pprint.pformat(req)}")
    r = requests.post(
        SECS_KVM_CMD_ENDPOINT,
        json=req,
    )
    r.raise_for_status()
    return r.json()


@blp.cli.command("req-evt-rpt")
@click.argument("ceid", type=int)
def req_evt_rpt(ceid: int):
    logger.info(f"req_evt_rpt: {ceid}")
    result = request_event_report(ceid)
    logger.info(f"{result}")


@blp.cli.command("req-ant-rpt")
@click.argument("rptid", type=int)
def req_ant_rpt(rptid: int):
    logger.info(f"req_ant_rpt: {rptid}")
    result = request_annotated_event_report(rptid)
    logger.info(f"{result}")


@blp.cli.command("link-evt-rpt")
@click.argument("ceid", type=int)
@click.argument("reports")
def link_evt_rpt(ceid: int, reports: str):
    logger.info(f"link_evt_rpt: {ceid=}, {reports=}")
    rptids = request_event_report(ceid)
    logger.info(f"origin rptids: {rptids=}")
    rptids.extend([int(v) for v in reports.split(",")])
    logger.info(f"update rptids to: {rptids=}")

    result = link_event_report_reset(ceid)
    logger.info(f"reset: {result}")
    result = link_event_report(ceid, rptids)
    logger.info(f"{result}")


@blp.cli.command("def-rpt")
@click.argument("rptid", type=int)
@click.argument("svids")
def def_rpt(rptid: int, svids: str):
    logger.info(f"def-rpt: {rptid=}, {svids=}")
    sv_ids = [int(v) for v in svids.split(",")]
    define_report_reset(rptid)
    result = define_report(rptid, sv_ids)
    logger.info(f"{result}")


@blp.cli.command("init-defaults")
def init_defaults():
    # PM1
    ceid = PM1_RECIPE_STEP_STATISTICS_CEID
    rptid = PM1_RECIPE_STEP_INFO_RPID
    sv_ids = list(pm1_svid_dvid_defaults.keys())
    logger.info(f"[PM1] init-defaults: {ceid=}, {rptid=}, {sv_ids=}")
    define_report_reset(rptid)
    result = define_report(rptid, sv_ids)
    logger.info(f"{result}")

    rptids = request_event_report(ceid)
    logger.info(f"origin rptids: {rptids=}")
    rptids.extend([rptid])
    logger.info(f"update rptids to: {rptids=}")

    result = link_event_report_reset(ceid)
    logger.info(f"reset: {result}")
    result = link_event_report(ceid, rptids)
    logger.info(f"{result}")

    # PM2
    ceid = PM2_RECIPE_STEP_STATISTICS_CEID
    rptid = PM2_RECIPE_STEP_INFO_RPID
    sv_ids = list(pm2_svid_dvid_defaults.keys())
    logger.info(f"[PM2] init-defaults: {ceid=}, {rptid=}, {sv_ids=}")
    result = define_report_reset(rptid)
    logger.info(f"reset: {result}")
    result = define_report(rptid, sv_ids)
    logger.info(f"{result}")

    rptids = request_event_report(ceid)
    logger.info(f"origin rptids: {rptids=}")
    rptids.extend([rptid])
    logger.info(f"update rptids to: {rptids=}")

    result = link_event_report_reset(ceid)
    logger.info(f"reset: {result}")
    result = link_event_report(ceid, rptids)
    logger.info(f"{result}")


pm1_svid_dvid_defaults = {
    4: "Clock",
    110000005: "PM1/Recipe.Step",
    110000009: "PM1/Recipe.Message_dv",
    1023004: "PM1/JobID",
    217001315: "PM1/Ceiling.temp.mean",
    218001315: "PM1/Reactor.temp.mean",
    212121005: "PM1/EpiTrueTemp1.mean",
    212122005: "PM1/EpiTrueTemp2.mean",
    212123005: "PM1/EpiTrueTemp3.mean",
    212124005: "PM1/EpiTrueTemp4.mean",
    212125005: "PM1/EpiTrueTemp5.mean",
    212126005: "PM1/EpiTrueTemp6.mean",
    212968005: "PM1/SumMO1.mean",
    211103045: "PM1/TMAl_1.source",
    211101045: "PM1/TMGa_2.source.mean",
}

pm2_svid_dvid_defaults = {
    4: "Clock",
    1110000005: "PM2/Recipe.Step",
    1110000009: "PM2/Recipe.Message_dv",
    2023004: "PM2/JobID",
    1217001315: "PM2/Ceiling.temp.mean",
    1212121005: "PM2/EpiTrueTemp1.mean",
    1212122005: "PM2/EpiTrueTemp2.mean",
    1212123005: "PM2/EpiTrueTemp3.mean",
    1212124005: "PM2/EpiTrueTemp4.mean",
    1212125005: "PM2/EpiTrueTemp5.mean",
    1212126005: "PM2/EpiTrueTemp6.mean",
    1212968005: "PM2/SumMO1.mean",
    1211103045: "PM2/TMAl_1.source",
    1211101045: "PM2/TMGa_2.source.mean",
}

# def SF_NAME(sf: StreamFunction):
#     return f"S{sf.Stream}F{sf.Function}"


# subject = "eap.event.TM1"
# headers = {
#     "Context-Type": "application/json",
#     "ID": "1978757960607727616",
#     "Version": "2.1.6",
# }
# reply = ""
# data = {
#     "Eqp": "TM1",
#     "Name": "SecsMessageRecv",
#     "Value": {
#         "PortID": 100,
#         "SessionID": 0,
#         "SystemID": 1842020352,
#         "Stream": 6,
#         "Function": 11,
#         "FmtType": "sdl",
#         "Data": {
#             "DATAID": 0,
#             "CEID": 1130000036,
#             "DATA": [
#                 {
#                     "V": ["2025101617373160", 41, "Turn off MAINTENANCE Mode"],
#                     "RPTID": 1150000001,
#                 }
#             ],
#         },
#     },
#     "Type": 0,
# }
