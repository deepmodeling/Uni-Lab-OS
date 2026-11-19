"""可供 Edge、微后端与 Adapter 共同引用的资源领域对象。"""

from unilabos.resources.objects.base import ResourceObject
from unilabos.resources.objects.pose import (
    ResourceDictPosition,
    ResourceDictPositionObject,
    ResourceDictPositionObjectType,
    ResourceDictPositionScale,
    ResourceDictPositionScaleType,
    ResourceDictPositionSize,
    ResourceDictPositionSizeType,
    ResourceDictPositionType,
)
from unilabos.resources.objects.joint_state import (
    ResourceJointState,
    ResourceJointStateType,
)
from unilabos.resources.objects.resource import (
    EXTRA_CLASS,
    EXTRA_RESOURCE_CLASS,
    EXTRA_RESOURCE_META_DATA,
    EXTRA_RESOURCE_JOINT_STATE,
    EXTRA_RESOURCE_POSE,
    EXTRA_SAMPLE_UUID,
    EXTRA_SITES,
    EXTRA_UNILABOS_SAMPLE_UUID,
    FRONTEND_POSE_EXTRA,
    PLR_CONFIG_ROOT_KEYS,
    RESOURCE_ROOT_FIELDS,
    ResourceDict,
    ResourceDictType,
    assemble_tracker_state,
)
from unilabos.resources.objects.sample import LabSample, SampleUUIDsType
from unilabos.resources.objects.state import (
    LiquidHistoryEntry,
    LiquidStateEntry,
    TRACKER_STATE_KEYS,
)
from unilabos.resources.objects.site import (
    ResourceSite,
    ResourceSiteType,
    SiteDefinition,
    SiteDefinitionInput,
    normalize_available_sites,
    validate_instantiated_sites,
)

__all__ = [
    "LiquidHistoryEntry",
    "LiquidStateEntry",
    "LabSample",
    "EXTRA_CLASS",
    "EXTRA_RESOURCE_CLASS",
    "EXTRA_RESOURCE_META_DATA",
    "EXTRA_RESOURCE_JOINT_STATE",
    "EXTRA_RESOURCE_POSE",
    "EXTRA_SAMPLE_UUID",
    "EXTRA_SITES",
    "EXTRA_UNILABOS_SAMPLE_UUID",
    "FRONTEND_POSE_EXTRA",
    "PLR_CONFIG_ROOT_KEYS",
    "RESOURCE_ROOT_FIELDS",
    "ResourceDict",
    "ResourceDictType",
    "ResourceJointState",
    "ResourceJointStateType",
    "ResourceDictPosition",
    "ResourceDictPositionObject",
    "ResourceDictPositionObjectType",
    "ResourceDictPositionScale",
    "ResourceDictPositionScaleType",
    "ResourceDictPositionSize",
    "ResourceDictPositionSizeType",
    "ResourceDictPositionType",
    "ResourceObject",
    "ResourceSite",
    "ResourceSiteType",
    "SiteDefinition",
    "SiteDefinitionInput",
    "SampleUUIDsType",
    "TRACKER_STATE_KEYS",
    "assemble_tracker_state",
    "normalize_available_sites",
    "validate_instantiated_sites",
]
