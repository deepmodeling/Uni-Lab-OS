from pydantic import BaseModel, Field


class RespCode:
    Success = 0

    ErrorHostNotInit = 2001  # Host node not initialized
    ErrorInvalidReq = 2002  # Invalid request data


class DeviceAction(BaseModel):
    x: str
    y: str
    action: str


class Device(BaseModel):
    id: str
    name: str
    action: DeviceAction


class DeviceList(BaseModel):
    items: list[Device] = []
    page: int
    pageSize: int


class DevicesResponse(BaseModel):
    code: int
    data: DeviceList


class DeviceInfoResponse(BaseModel):
    code: int
    data: Device


class PageResp(BaseModel):
    item: list = []
    page: int = 1
    pageSize: int = 10


class Resp(BaseModel):
    code: int = RespCode.Success
    data: dict = {}
    message: str = "success"


class JobAddReq(BaseModel):
    device_id: str = Field(examples=["Gripper"], description="device id")
    action: str = Field(examples=["_execute_driver_command_async"], description="action name", default="")
    action_type: str = Field(
        examples=["unilabos_msgs.action._str_single_input.StrSingleInput"], description="action type", default=""
    )
    action_args: dict = Field(examples=[{"string": "string"}], description="action arguments", default_factory=dict)
    task_id: str = Field(examples=["task_id"], description="task uuid (auto-generated if empty)", default="")
    job_id: str = Field(examples=["job_id"], description="goal uuid (auto-generated if empty)", default="")
    node_id: str = Field(examples=["node_id"], description="node uuid", default="")
    server_info: dict = Field(
        examples=[{"send_timestamp": 1717000000.0}],
        description="server info (auto-generated if empty)",
        default_factory=dict,
    )

    data: dict = Field(examples=[{"position": 30, "torque": 5, "action": "push_to"}], default_factory=dict)


class JobStepFinishReq(BaseModel):
    token: str = Field(examples=["030944"], description="token")
    request_time: str = Field(examples=["2024-12-12 12:12:12.xxx"], description="requestTime")
    data: dict = Field(
        examples=[
            {
                "orderCode": "任务号。字符串",
                "orderName": "任务名称。字符串",
                "stepName": "步骤名称。字符串",
                "stepId": "步骤Id。GUID",
                "sampleId": "通量Id。GUID",
                "startTime": "开始时间。时间格式",
                "endTime": "完成时间。时间格式",
            }
        ]
    )


class JobPreintakeFinishReq(BaseModel):
    token: str = Field(examples=["030944"], description="token")
    request_time: str = Field(examples=["2024-12-12 12:12:12.xxx"], description="requestTime")
    data: dict = Field(
        examples=[
            {
                "orderCode": "任务号。字符串",
                "orderName": "任务名称。字符串",
                "sampleId": "通量Id。GUID",
                "startTime": "开始时间。时间格式",
                "endTime": "完成时间。时间格式",
                "Status": "通量状态,0待生产、2进样、10开始、完成20、异常停止-2、人工停止或取消-3",
            }
        ]
    )


class JobFinishReq(BaseModel):
    token: str = Field(examples=["030944"], description="token")
    request_time: str = Field(examples=["2024-12-12 12:12:12.xxx"], description="requestTime")
    data: dict = Field(
        examples=[
            {
                "orderCode": "任务号。字符串",
                "orderName": "任务名称。字符串",
                "startTime": "开始时间。时间格式",
                "endTime": "完成时间。时间格式",
                "status": "通量状态,完成30、异常停止-11、人工停止或取消-12",
                "usedMaterials": [
                    {
                        "materialId": "物料Id。GUID",
                        "locationId": "库位Id。GUID",
                        "typeMode": "物料类型。 样品1、试剂2、耗材0",
                        "usedQuantity": "使用的数量。 数字",
                    }
                ],
            }
        ]
    )


class JobData(BaseModel):
    jobId: str = Field(examples=["sfsfsfeq"], description="goal uuid")
    status: int = Field(
        examples=[0, 1],
        default=0,
        description="0:UNKNOWN, 1:ACCEPTED, 2:EXECUTING, 3:CANCELING, 4:SUCCEEDED, 5:CANCELED, 6:ABORTED",
    )
    result: dict = Field(
        default_factory=dict,
        description="Job result data (available when status is SUCCEEDED/CANCELED/ABORTED)",
    )


class JobStatusResp(Resp):
    data: JobData


class JobAddResp(Resp):
    data: JobData
