# Unreal Builder · by Za0Shu1

一个用于 **Unreal Engine 项目编译与打包** 的 Windows 桌面小工具（Python + Tkinter 编写，PyInstaller 打包为单文件 exe）。

## 功能

- **工程选择**：扫描文件夹自动发现 `.uproject`（Scan Folder，持久化上次扫描目录），或手动 Browse 指定（默认定位到上次扫描目录，选中工程会记住）；选中的工程路径自动持久化，下次启动直接恢复
- **引擎自动定位**：读取 `.uproject` 的 `EngineAssociation`，通过注册表自动找到对应引擎的 `Build.bat` / `RunUAT.bat`
- **Compile**：调用引擎 `Build.bat` 编译当前工程（默认 Editor 目标），适合日常编译后打开 UE 编辑器
- **Package**：调用 `RunUAT.bat BuildCookRun` 完整打包（Cook + Stage + Pak），打包输出目录持久化
- **编译/打包完成提示音**：成功播放清脆提示音，失败播放警告音
- **打包完成快捷打开**：成功后界面出现 **Open Output** 按钮（1 分钟后自动隐藏），点击直达打包输出目录
- **实时日志**：编译/打包日志流式输出，支持右键复制 / 全选

## 使用

```
1. 打开 UnrealBuilder.exe
2. Scan Folder... 选择工程所在目录（自动发现所有 .uproject）
   或 Browse... 手动选择工程文件
3. 选择平台（Win64 等）与配置（Development 等）
4. 点击 Compile 编译 / Package 打包
```

## 界面

| 区域 | 说明 |
| --- | --- |
| Unreal Project | 工程下拉框 + Scan Folder / Browse 按钮 |
| Engine | 自动识别到的引擎根目录 |
| Platform / Config | 目标平台与编译配置 |
| Compile | 编译工程（默认 Editor 目标） |
| Package | 完整打包（弹出目录选择，默认上次打包目录或工程根目录） |
| Open Output | 打包成功后出现 1 分钟，点击打开输出目录 |

## 打包本项目为 exe

```bash
python -m PyInstaller --noconfirm --onefile --windowed --name UnrealBuilder ^
  --icon "Default.ico" --add-data "Default.ico;." ^
  --distpath "dist" --workpath "build" --specpath "." UnrealBuilder.py
```

生成的 exe 位于 `dist/UnrealBuilder.exe`。

## 配置持久化

保存在 `%APPDATA%\UnrealBuilder\unreal_builder_config.json`：

- `last_project` / `projects`：上次选中的工程、项目列表
- `scan_dir`：上次 Scan 的根目录（与 Package 路径相互独立）
- `package_outputs`：**按工程分别持久化**的打包输出目录（key 为 uproject 路径），每个工程记住自己的输出路径，未设置时默认该工程根目录
- 重新 Scan 会以新扫描结果**替换**整个项目列表（相当于过滤器），而不是累加

## 环境

- Windows + Python 3.12（运行 exe 无需安装 Python）
- PyInstaller 6.x（打包时需要）
- 使用场景：安装了 Unreal Engine（Launcher 注册表版或源码版）的工程