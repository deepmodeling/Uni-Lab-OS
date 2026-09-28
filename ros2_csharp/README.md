## ros2 C# 开发指南

## 背景
    该项目是一个示例项目，帮您快速的配置 C# 开发环境。具体硬件设备驱动需要您自行编写。


## C# 开发环境配置
### 1. 安装 dotnet 环境
dotnet [下载地址](https://dotnet.microsoft.com/zh-cn/download)
安装完成后执行一下命令，如果安装成功会有如下展示。
```
  dotnet --version
  8.0.406
```

### 2. ros2-humble 安装 
    请参照官方[安装文档](https://docs.ros.org/en/humble/Installation/Windows-Install-Binary.html)

### 3. 开发配置
1. ros2_dotnet 环境配置请参阅[配置文档](https://github.com/ros2-dotnet/ros2_dotnet/blob/main/README.md)。
2. 参照项目下的 ros_driver 创建您自己的项目。
3. 编写相应的代码。
4. 构建流程
 ```
 打开 windows 搜索 Developer Command Prompt  启动 visual studio 
 cd {本地ros2-humble} 目录
 call ./local_setup.bat
 cd {Uni-Lab-OS 项目目录}/ros_csharp
 vcs import ./src < ros2_humble.repos
 colcon build --merge-install --packages-up-to {your package name} --event-handlers console_cohesion+
 ```
5. 拷贝编译完成的包到conda 对应的虚拟环境目录下。
6. 执行如下命令运行测试

 ```
  参照 ros_driver 项目，需要配置 CONFIG_FILE_PATH 环境变量，对应的值是 {Uni-Lab-OS 路径}/unilabos/registry/devices 下的 yaml 文件路径。
  ros2 run {项目包名} {可执行程序名}
 ```