cmake_minimum_required(VERSION 3.16)
project(unilabos_msgs)

# Default to C99
if(NOT CMAKE_C_STANDARD)
  set(CMAKE_C_STANDARD 99)
endif()

# Default to C++14
if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 14)
endif()

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
  add_compile_options(-include cstdint)
endif()

# find dependencies
find_package(ament_cmake REQUIRED)
find_package(std_msgs REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(geometry_msgs REQUIRED)

set(action_files
  "action/SendCmd.action"
  "action/Stir.action"
  "action/HeatChill.action"
  "action/HeatChillStart.action"
  "action/HeatChillStop.action"
  "action/AdjustPH.action"
  "action/ResetHandling.action"
  "action/Dry.action"
  "action/Hydrogenate.action"
  "action/Recrystallize.action"
  "action/CleanVessel.action"
  "action/Dissolve.action"
  "action/FilterThrough.action"
  "action/RunColumn.action"
  "action/Wait.action"
  "action/WashSolid.action"
  "action/Filter.action"
  "action/Add.action"
  "action/AddSolid.action"
  "action/Centrifuge.action"
  "action/Crystallize.action"
  "action/Purge.action"
  "action/StartPurge.action"
  "action/StartStir.action"
  "action/StopPurge.action"
  "action/StopStir.action"
  "action/Transfer.action"
  "action/SetPumpPosition.action"
  "action/LiquidHandlerProtocolCreation.action"
  "action/LiquidHandlerAspirate.action"
  "action/LiquidHandlerDiscardTips.action"
  "action/LiquidHandlerDispense.action"
  "action/LiquidHandlerDropTips.action"
  "action/LiquidHandlerDropTips96.action"
  "action/LiquidHandlerMoveLid.action"
  "action/LiquidHandlerMovePlate.action"
  "action/LiquidHandlerMoveResource.action"
  "action/LiquidHandlerPickUpTips.action"
  "action/LiquidHandlerPickUpTips96.action"
  "action/LiquidHandlerReturnTips.action"
  "action/LiquidHandlerReturnTips96.action"
  "action/LiquidHandlerSetLiquid.action"
  "action/LiquidHandlerSetTipRack.action"
  "action/LiquidHandlerStamp.action"
  "action/LiquidHandlerTransfer.action"
  "action/LiquidHandlerTransferBiomek.action"
  "action/LiquidHandlerIncubateBiomek.action"
  "action/LiquidHandlerMoveBiomek.action"
  "action/LiquidHandlerOscillateBiomek.action"
  "action/LiquidHandlerAdd.action"
  "action/LiquidHandlerMix.action"
  "action/LiquidHandlerMoveTo.action"
  "action/LiquidHandlerRemove.action"

  "action/EmptyIn.action"
  "action/FloatSingleInput.action"
  "action/IntSingleInput.action"
  "action/StrSingleInput.action"
  "action/Point3DSeparateInput.action"

  "action/ResourceCreateFromOuter.action"
  "action/ResourceCreateFromOuterEasy.action"

  "action/SolidDispenseAddPowderTube.action"

  "action/PumpTransfer.action"
  "action/Clean.action"
  "action/Separate.action"
  "action/Evaporate.action"
  "action/EvacuateAndRefill.action"

  "action/WorkStationRun.action"
  "action/AGVTransfer.action"

  "action/DispenStationSolnPrep.action"
  "action/DispenStationVialFeed.action"

  "action/PostProcessGrab.action"
  "action/PostProcessTriggerClean.action"
  "action/PostProcessTriggerPostPro.action"

  "action/ReactionStationDripBack.action"
  "action/ReactionStationLiquidFeedBeaker.action"
  "action/ReactionStationLiquidFeedSolvents.action"
  "action/ReactionStationLiquidFeedTitration.action"
  "action/ReactionStationLiquidFeedVialsNonTitration.action"
  "action/ReactionStationProExecu.action"
  "action/ReactionStationReactorTakenOut.action"
  "action/ReactionStationReaTackIn.action"
  "action/ReactionStationSolidFeedVial.action"
)

set(srv_files
  "srv/Stop.srv"
  "srv/SerialCommand.srv"
  "srv/ResourceGet.srv"
  "srv/ResourceList.srv"
  "srv/ResourceAdd.srv"
  "srv/ResourceUpdate.srv"
  "srv/ResourceDelete.srv"
)

set(msg_files
  "msg/State.msg"
  "msg/Resource.msg"
)

rosidl_generate_interfaces(${PROJECT_NAME}
  ${msg_files}
  ${srv_files}
  ${action_files}
  DEPENDENCIES std_msgs geometry_msgs
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  # the following line skips the linter which checks for copyrights
  # uncomment the line when a copyright and license is not present in all source files
  #set(ament_cmake_copyright_FOUND TRUE)
  # the following line skips cpplint (only works in a git repo)
  # uncomment the line when this package is not in a git repo
  #set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
