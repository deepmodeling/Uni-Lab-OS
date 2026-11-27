cmake_minimum_required(VERSION 3.20)
project(unilab_bullet_standalone LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

option(UNILAB_BULLET_THREADSAFE "按 BT_THREADSAFE 构建的 Bullet 链接（conda float64 默认开启）" OFF)
option(UNILAB_BULLET_BUILD_SMOKE_TEST "构建 C++ 最小闭环冒烟测试" OFF)
option(UNILAB_BULLET_ALLOW_FLOAT_LIBS "允许链接非 float64 后缀的 Bullet（仅调试）" OFF)

set(UNILAB_BULLET_INCLUDE_DIR "" CACHE PATH "包含 btBulletDynamicsCommon.h 的目录")
set(UNILAB_BULLET_LIBRARY_DIR "" CACHE PATH "包含 Bullet*-float64 库的目录")

if(NOT UNILAB_BULLET_INCLUDE_DIR OR NOT UNILAB_BULLET_LIBRARY_DIR)
  message(FATAL_ERROR
    "需要 -DUNILAB_BULLET_INCLUDE_DIR=<...>/include/bullet 与 "
    "-DUNILAB_BULLET_LIBRARY_DIR=<...>/lib。可用 conda-forge 的 bullet-cpp 提供。")
endif()
if(NOT EXISTS "${UNILAB_BULLET_INCLUDE_DIR}/btBulletDynamicsCommon.h")
  message(FATAL_ERROR "找不到 ${UNILAB_BULLET_INCLUDE_DIR}/btBulletDynamicsCommon.h")
endif()

set(UNILAB_BULLET_LIBRARIES "")
foreach(_component BulletDynamics BulletCollision LinearMath)
  find_library(UNILAB_${_component}_LIBRARY
    NAMES ${_component}-float64 ${_component}
    PATHS "${UNILAB_BULLET_LIBRARY_DIR}"
    NO_DEFAULT_PATH)
  if(NOT UNILAB_${_component}_LIBRARY)
    message(FATAL_ERROR
      "缺少 ${_component}（查找 ${_component}-float64 / ${_component}，目录 "
      "${UNILAB_BULLET_LIBRARY_DIR}）。请安装 conda-forge bullet-cpp 的 float64 变体。")
  endif()
  if(NOT UNILAB_BULLET_ALLOW_FLOAT_LIBS AND NOT UNILAB_${_component}_LIBRARY MATCHES "float64")
    message(FATAL_ERROR
      "${UNILAB_${_component}_LIBRARY} 不是 float64 变体；独立桥要求 BT_USE_DOUBLE_PRECISION。")
  endif()
  list(APPEND UNILAB_BULLET_LIBRARIES "${UNILAB_${_component}_LIBRARY}")
endforeach()

# BulletDynamics 的部分符号落在 Bullet3Common；存在就一并链接。
find_library(UNILAB_Bullet3Common_LIBRARY
  NAMES Bullet3Common-float64 Bullet3Common
  PATHS "${UNILAB_BULLET_LIBRARY_DIR}"
  NO_DEFAULT_PATH)
if(UNILAB_Bullet3Common_LIBRARY)
  list(APPEND UNILAB_BULLET_LIBRARIES "${UNILAB_Bullet3Common_LIBRARY}")
endif()

add_library(unilab_bullet_standalone SHARED
  src/rb_compat.cpp
  src/lab_extras.cpp)
target_include_directories(unilab_bullet_standalone PRIVATE "${UNILAB_BULLET_INCLUDE_DIR}")
target_compile_definitions(unilab_bullet_standalone PRIVATE BT_USE_DOUBLE_PRECISION)
if(UNILAB_BULLET_THREADSAFE)
  target_compile_definitions(unilab_bullet_standalone PRIVATE BT_THREADSAFE=1)
endif()
target_link_libraries(unilab_bullet_standalone PRIVATE ${UNILAB_BULLET_LIBRARIES})

set_target_properties(unilab_bullet_standalone PROPERTIES
  PREFIX ""
  OUTPUT_NAME "unilab_bullet_standalone")

if(MSVC)
  target_compile_options(unilab_bullet_standalone PRIVATE /utf-8 /EHsc)
endif()

if(UNIX)
  find_package(Threads REQUIRED)
  target_link_libraries(unilab_bullet_standalone PRIVATE Threads::Threads)
  set_target_properties(unilab_bullet_standalone PROPERTIES
    BUILD_RPATH "${UNILAB_BULLET_LIBRARY_DIR}"
    INSTALL_RPATH "${UNILAB_BULLET_LIBRARY_DIR}")
endif()

if(UNILAB_BULLET_BUILD_SMOKE_TEST)
  add_executable(unilab_bullet_smoke_test tests/smoke_test.cpp)
  target_link_libraries(unilab_bullet_smoke_test PRIVATE unilab_bullet_standalone)
  set_target_properties(unilab_bullet_smoke_test PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}")
endif()
