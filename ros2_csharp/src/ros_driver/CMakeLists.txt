cmake_minimum_required(VERSION 3.5)

project(ros_driver C)

find_package(ament_cmake REQUIRED)
find_package(rcldotnet REQUIRED)

find_package(rcldotnet_common REQUIRED)

find_package(builtin_interfaces REQUIRED)
find_package(std_msgs REQUIRED)
find_package(std_srvs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(test_msgs REQUIRED)
find_package(action_msgs REQUIRED)  # Add this line
find_package(rcldotnet REQUIRED)
find_package(ilabos_msgs REQUIRED)



find_package(dotnet_cmake_module REQUIRED)

set(CSHARP_TARGET_FRAMEWORK "netcoreapp6.0")
find_package(DotNETExtra REQUIRED)


# 使用相对路径定位 YamlDotNet.dll
set(YAMLDOTNET_DLL "${CMAKE_CURRENT_SOURCE_DIR}/deps/YamlDotNet/YamlDotNet.dll")

# 检查文件是否存在
if(NOT EXISTS ${YAMLDOTNET_DLL})
  message(FATAL_ERROR "YamlDotNet.dll not found at: ${YAMLDOTNET_DLL}")
endif()


set(_assemblies_dep_dlls
    ${rcldotnet_common_ASSEMBLIES_DLL}
    ${rcldotnet_ASSEMBLIES_DLL}
    ${builtin_interfaces_ASSEMBLIES_DLL}
    ${std_msgs_ASSEMBLIES_DLL}
    ${std_srvs_ASSEMBLIES_DLL}
    ${geometry_msgs_ASSEMBLIES_DLL}
    ${test_msgs_ASSEMBLIES_DLL}
    ${action_msgs_ASSEMBLIES_DLL}
    ${ilabos_msgs_ASSEMBLIES_DLL}
    ${YAMLDOTNET_DLL}
)

add_dotnet_executable(ros_driver
  Program.cs
  DeviceNode.cs
  DeviceActionServer.cs
  PropertyPublisher.cs
  MockGripper.cs
  utils/Yaml.cs
  utils/MessageConverter.cs
  INCLUDE_DLLS
  ${_assemblies_dep_dlls}
)


install_dotnet(ros_driver DESTINATION lib/${PROJECT_NAME}/dotnet)


ament_package()