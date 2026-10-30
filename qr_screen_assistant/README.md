# 屏幕区域二维码实时捕获助手

Windows 桌面工具：持续监控你框选的一小块屏幕区域，在本地识别二维码，自动保存该区域的原始截图，并把二维码裁剪、放大显示在窗口右侧，方便用手机扫描。

- 截图和二维码识别全部在本地完成，应用代码不包含任何上传/联网请求。
- 不自动打开二维码内容、不自动登录、不模拟手机操作、不保存账号密码。
- 只保留当前运行的最近 50 条历史记录；截图文件保存到本机 `captures/`。
- 单文件实现，方便直接运行；PyInstaller 打包说明见后文。

> **免责声明：仅用于自己核对二维码。请勿用于未经授权的账号扫码、登录，或规避平台规则。**
>
> 二维码可能指向登录链接、付款页面或其他敏感操作。本软件只做“截图 + 识别 + 显示 + 复制文本”，不会替你执行任何扫码或登录动作。请自行确认二维码来源和用途。

---

## 1. 功能

- **屏幕区域框选**
  - 点击按钮后主窗口自动隐藏，用透明置顶层在屏幕上拖拽框选。
  - 支持多显示器：每个显示器上都可以框选。
  - 正确处理 Windows 高 DPI：使用 Win32 `HMONITOR` 与物理像素坐标进行换算，避免 125%/150% 缩放下截图区域偏移。
  - 框选后显示区域设备、物理像素尺寸和左上角坐标。
- **实时本地识别**
  - 默认每 **150 ms** 截取并检测一次。
  - `mss` 高速截图 + OpenCV `QRCodeDetector` 检测/解码，Pillow 保存原始截图。
  - 后台线程工作，Qt 信号槽回到主线程更新界面，不卡 UI。
  - 检测到二维码后：保存原始区域截图 → 裁剪二维码 → 放大显示 → 显示解码文本 → 加入历史。
  - 左侧约 1 帧/秒的低频区域预览，不影响后台识别频率。
- **去重**
  - 相同内容在 2 秒内重复出现：只更新历史记录中的“最近识别”时间，不新增记录。
  - 同一二维码持续留在画面中：不会每 150 ms 重复截图和刷屏；时间会继续更新。
  - 二维码离开画面一段时间后再次出现，且距上次保存已超过 2 秒，可新增一条真实的新记录。
- **右侧大图区**
  - 按角点做透视校正，裁剪二维码并增加白色静区，放大到清晰可扫的尺寸。
  - 保持原始比例显示；二维码消失后保留最近一次结果。
  - “保存当前二维码图片”与“复制二维码内容”均需手动触发。
  - 二维码内容在只读文本框中显示，不会自动打开或访问。
- **历史记录**
  - 数字时间、内容摘要、缩略图；点击任意记录在右侧查看大图。
  - 最多保留最近 50 条。
  - 清空历史不会删除已经保存的截图文件。
- **控制与状态**
  - 开始/暂停监控、选择监控区域、清空历史、打开截图目录、窗口置顶开关。
  - 实时显示采集 FPS、平均检测耗时、当前扫描/检测状态。
  - 截图失败、区域越界、显示器变化、解码失败等均有状态提示或降级处理。

---

## 2. 项目结构

```text
qr_screen_assistant/
├─ main.py              # 单文件完整程序（GUI + 框选 + 截图线程 + 二维码识别）
├─ requirements.txt     # 固定推荐版本的运行依赖
├─ README.md            # 本文件
├─ build_exe.ps1        # 可选的 PyInstaller 打包脚本
├─ .gitignore           # 忽略虚拟环境、构建产物和本地截图
├─ assets/
│  ├─ app.ico           # Windows 可执行文件图标
│  └─ app.png           # 图标预览
└─ captures/            # 首次运行时自动创建，保存原始区域截图
```

`captures/` 不随项目提交；应用会在首次启动时自动创建它。打包为 exe 后，截图目录在 exe 旁边（若该目录不可写，会自动回退到用户目录，并在界面状态栏提示）。

---

## 3. 系统要求与安装

### 要求

- Windows 10 / Windows 11，64 位
- Python **3.10–3.13**（推荐 3.12；更新版本的 Python 可能需要调整依赖版本）
- 可正常截图的桌面会话（锁屏、UAC 安全桌面、部分受 DRM 保护的窗口无法截取）
- 建议屏幕分辨率不低于 1280×720；高 DPI 小屏下主界面提供纵向滚动条

### 安装步骤（PowerShell）

```powershell
# 请把下面的路径替换成你实际保存项目的目录
cd "D:\Tools\qr_screen_assistant"

py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

python main.py
```

如果 PowerShell 禁止执行激活脚本，可以临时绕过：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

也可以完全不激活虚拟环境，直接用虚拟环境的 Python：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

> 关键点：**安装依赖和运行程序必须使用同一个 Python 环境**。如果 `python main.py` 报 `ModuleNotFoundError`，先用 `python -c "import sys; print(sys.executable)"` 确认解释器路径。

### 固定版本说明

`requirements.txt` 固定了以下已验证版本，避免 Qt/OpenCV 更新造成 API 或 DLL 兼容问题：

| 依赖 | 固定版本 | 作用 |
|---|---|---|
| PySide6 | 6.8.3 | 桌面 GUI、线程/信号、框选置顶层 |
| mss | 10.0.0 | 高速屏幕区域截图 |
| opencv-python | 4.11.0.86 | 二维码检测与解码 |
| Pillow | 11.2.1 | 原始截图 PNG 保存 |
| numpy | 2.2.6 | 图像数组运算 |

这些版本都提供 Windows x64 轮子。Python 3.10/3.11/3.12/3.13 通常都可以安装；如你的环境确实没有对应轮子，再改用该 Python 版本支持的最新兼容版本。

> 不要同时安装 `opencv-python` 和 `opencv-contrib-python`，二者会覆盖同一个 `cv2` 包目录。基础安装只需要前者。

---

## 4. 使用方法

1. 启动程序：

   ```powershell
   python main.py
   ```

2. 点击 **“选择监控区域”**。
   - 主窗口会自动隐藏。
   - 在任意一个显示器上按住鼠标左键拖拽，框选二维码可能出现的区域。
   - 松开鼠标即可完成选择；主窗口重新出现并自动开始监控。
   - 按 `Esc` 或点鼠标右键可取消框选。
   - 框选时顶部会显示当前区域的物理像素尺寸。

3. 让二维码出现在框选区域内。
   - 检测到后，右侧立即显示放大的二维码。
   - 原始区域截图自动保存到 `captures/`，文件名形如 `capture_20261030_182204_667.png`。
   - 同一二维码持续出现时会更新历史时间，但不会重复刷出记录和截图。

4. 用手机扫描右侧大图。
   - 需要留档时点击 **“保存当前二维码图片”**（默认建议保存为 PNG）。
   - 需要文本时点击 **“复制二维码内容”**，然后自行粘贴到记事本等工具中核对。
   - 软件不会自动打开链接或执行任何网页操作。

5. 其他操作：
   - **暂停监控**：停止后台截图，降低 CPU；已选择区域会保留。
   - **继续监控**：使用当前区域继续。
   - **窗口置顶**：把主窗口固定在其他窗口上方，便于边看视频边扫描。
   - **打开截图目录**：用资源管理器打开真实截图目录。
   - **清空历史**：清空当前会话的 50 条记录，不会删除磁盘截图。

### 区域尺寸与多显示器说明

- 一次框选只覆盖**一个显示器内的一个矩形**。你可以对任意显示器分别框选，但不支持一次拖拽跨越多个显示器（跨屏矩形通常也没有必要，且会显著增加 CPU）。
- 使用多显示器时，程序通过 Qt 的屏幕对象和 Win32 显示器句柄进行匹配；不同缩放比例的显示器也可以正确换算截图坐标。
- 如果拖拽框选时显示器排列/缩放发生变化，或者显示器热插拔，请重新选择区域。程序会检测越界并提示，而不是在错误坐标上持续截图。
- **请把主窗口移到监控区域之外。** 框选时主窗口会隐藏，选完后会重新显示；如果它挡住了二维码区域，界面会用橙色文字提示。把主窗口移开后提示会自动消失。启用了“窗口置顶”时尤其要注意这一点。
- 主窗口被最小化时，屏幕监控仍会继续；右侧二维码和最新历史需要恢复窗口后才能看到。

### 关于 2 秒去重的精确行为

- 相同解码内容在 **2 秒内**再次被检测到时：更新已有记录的“最近识别”时间，不新增历史项，也不重复保存截图。
- 同一个二维码一直停留在区域里：视为同一事件，持续更新时间，不重复新增。
- 二维码消失超过约 1.2 秒，并在超过 2 秒后再次出现：按一次新的出现事件处理，可新增记录。
- “未解码但检测到二维码”的情况也会记录大图；其去重键是固定的，避免角点抖动导致不断新增记录。
- 如果两个不同二维码快速交替出现，超过 2 秒窗口后再次出现的内容会按新事件记录。

### 历史记录和文件

- 历史记录只在当前运行期间保留，关闭程序后清空。截图文件不会删除。
- 截图目录默认是程序旁的 `captures/`；如果那里不可写（例如程序放在 `Program Files`），会回退到：
  - `C:\Users\<你>\QrScreenCaptureAssistant\captures\`
  - 或系统临时目录下的 `QrScreenCaptureAssistant\captures\`（最后兜底）
- 当前截图目录会显示在“打开截图目录”按钮的提示中，以及界面底部的历史说明中。
- 关闭程序后重新打开，不会自动记住上次区域；点击“选择监控区域”重新框选即可。

---

## 5. 性能与实现说明

- 截图/检测在 `MonitorWorker(QThread)` 后台线程；主线程只做界面绘制。
- 每帧流程：`mss.grab()` → NumPy BGR → OpenCV 灰度检测/解码 → 必要时生成本地预览/保存截图 → 通过 Qt Signal 投递。
- 界面上显示：
  - **采集 FPS**：实际有效循环帧率，默认理论上限约 6.7 FPS（150 ms 周期）。
  - **识别 ms**：OpenCV 检测/解码平均耗时。
  - **扫描状态**：正在扫描 / 检测到二维码 / 已暂停 / 发生错误。
- 预览是工作线程降采样后的低频画面（约 1 帧/秒），不会额外进行全速截图。
- 只有“形成新记录”的帧才会写 PNG 和生成历史缩略图，持续出现的二维码不会以 150 ms 频率写盘。
- 想进一步降低 CPU：
  - 框选尽量贴近二维码区域，不要框选整个视频窗口。
  - 使用暂停按钮。
  - 二维码本身在屏幕上越大、越清晰，检测越快且越稳定。
- 想调整采样周期，可修改 `main.py` 顶部的 `DEFAULT_INTERVAL_MS`（默认 150）。调大更省 CPU，但出现到显示的延迟会变长；调得太小可能超过单次检测耗时，实际 FPS 不会继续提高。

### 本次开发环境的实测

在一台 Windows 测试桌面上（1280×1024，DPR 1.0，Python 3.12.10）做了端到端验证：

- 520×360 的监控区域、150 ms 周期，实际采集约 **6.3–6.4 FPS**；
- 二维码从出现到主界面完成右侧显示约 **0.16 秒**（含截图、识别、保存 PNG 和跨线程投递）；
- 连续显示 4 秒以上只产生 **1 条历史记录**，但“最近识别”时间持续更新；
- 自动保存的原始截图尺寸与选取区域一致；
- 框选坐标映射、按 Esc 取消、取消后恢复监控、暂停/继续、无效区域错误提示均通过验证；
- 使用 **PyInstaller 6.14.2 单目录模式**打包成功，生成的 `QrScreenCaptureAssistant.exe` 可正常启动并显示主窗口。

实际延迟与 FPS 取决于 CPU、区域大小、二维码复杂度和磁盘速度；验收目标“0.5 秒内显示”在所选区域不是特别大时通常很容易达到。

---

## 6. 安全、隐私和使用边界

这是本工具最重要的部分，请在使用前阅读：

1. **只在本地处理**
   - 应用源码没有引入 `requests`、`urllib`、`socket` 等网络访问逻辑，也不会上传截图或二维码内容。
   - 所有文件只写入本机 `captures/` 和你手动选择的保存路径。
   - 复制内容只写入系统剪贴板；你可以随时清空剪贴板。

2. **不自动操作**
   - 不自动打开二维码里的网址。
   - 不自动扫码、不自动登录、不模拟手机点击、不调用支付接口。
   - 不保存任何账号、密码或登录令牌。
   - 历史记录只在内存中，不写入配置文件。

3. **请仅用于自己核对**
   - 仅用于核对你本来就有权查看的二维码，例如自己屏幕上出现的、你准备用手机扫描的码。
   - 请勿用于未经授权的账号扫码、登录、付款、绕过平台风控或其他违反服务条款的行为。
   - 某些平台可能禁止录屏、截屏或二维码辅助行为；请自行确认合规性。

4. **截图可能包含隐私**
   - 自动保存的是**整个框选区域**，可能包含聊天、账号、视频画面等周围信息。
   - 请保护好 `captures/` 目录，不要把它上传或分享给不可信的人。
   - 建议框选时尽量只包住二维码附近，使用完毕后及时清理不需要的截图。

5. **系统限制**
   - DRM/受保护内容、UAC 安全桌面、锁屏界面等可能截出黑屏或无画面，这是操作系统/内容保护限制，本软件不做绕过。
   - 本软件不帮助你规避平台规则，也不提供任何非法扫码能力。

---

## 7. 可选：启用 cv2.wechat_qrcode 备用解码

主识别路径固定使用 `cv2.QRCodeDetector`。基础安装的 `opencv-python` **不包含** contrib 模块，因此默认没有 `cv2.wechat_qrcode`。程序已做好可选适配：有模型时才会尝试备用解码，没有模型也不影响主功能。

如果你希望增加一个备用解码器，可按以下步骤。**这是可选的，且软件运行时不联网下载模型。**

### 7.1 替换为 OpenCV contrib 包

```powershell
# 先卸载基础包，避免两个包互相覆盖
python -m pip uninstall -y opencv-python
python -m pip install "opencv-contrib-python==4.11.0.86"
```

验证：

```powershell
python -c "import cv2; print(cv2.__version__, hasattr(cv2, 'wechat_qrcode'))"
```

应输出 `4.11.0` 和 `True`。

> 更换后不要再次安装 `requirements.txt` 中的基础 `opencv-python`，否则会覆盖 contrib 包。如果以后不需要 WeChat 备用解码，可卸载 contrib 并重新安装 `opencv-python==4.11.0.86`。

### 7.2 准备模型文件

从 OpenCV 官方资源获取 WeChatQRCode 模型（本项目不附带模型文件）：

- OpenCV 模型仓库：<https://github.com/opencv/opencv_3rdparty/tree/wechat_qrcode>
- OpenCV 官方教程：<https://docs.opencv.org/4.x/d5/dae/tutorial_wechat_qrcode.html>

将四个文件放到以下任意一个位置：

```text
qr_screen_assistant/
└─ models/
   └─ wechat_qrcode/
      ├─ detect.prototxt
      ├─ detect.caffemodel
      ├─ sr.prototxt
      └─ sr.caffemodel
```

程序还会搜索：

- 打包后 exe 旁的 `models\wechat_qrcode\`
- 当前工作目录的 `models\wechat_qrcode\`
- 环境变量 `QR_ASSISTANT_WECHAT_MODEL_DIR` 指向的目录

例如：

```powershell
$env:QR_ASSISTANT_WECHAT_MODEL_DIR = "D:\models\wechat_qrcode"
python main.py
```

如果没有模型文件，程序会静默跳过备用解码；主功能完全不受影响。模型文件各自受其原许可证约束，请自行确认授权与来源。程序只读取本地文件，不会下载。

---

## 8. 打包为 Windows exe（PyInstaller）

以下命令在项目目录中执行。建议使用与运行环境一致的 64 位 Python 和虚拟环境。

### 8.1 推荐：单目录打包（加载快、兼容性通常更好）

```powershell
python -m pip install "pyinstaller==6.14.2"

pyinstaller --noconfirm --clean --windowed --name QrScreenCaptureAssistant --icon "assets\app.ico" main.py
```

也可以直接运行脚本：

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

生成：

```text
dist\QrScreenCaptureAssistant\QrScreenCaptureAssistant.exe
```

请保留整个 `dist\QrScreenCaptureAssistant\` 目录。不要把 exe 单独复制到其他位置。

### 8.2 单文件 exe

```powershell
pyinstaller --noconfirm --clean --onefile --windowed --name QrScreenCaptureAssistant --icon "assets\app.ico" main.py
```

或：

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -OneFile
```

生成：

```text
dist\QrScreenCaptureAssistant.exe
```

单文件版启动时会先解压到临时目录，启动比单目录版慢；某些杀毒软件也更容易误报。

### 8.3 打包注意事项

- **不要跨平台打包**：在 Windows 上打包 Windows exe；使用 64 位 Python 和 64 位依赖。
- **captures 位置**：打包后 `get_app_directory()` 使用 exe 所在目录，因此 `captures/` 会出现在 exe 旁边。若放在 `C:\Program Files` 等不可写位置，程序会自动回退到用户目录并在状态栏提示。
- **如果 exe 启动时报 `cv2` 导入错误**，可尝试显式收集 OpenCV 包：

  ```powershell
  pyinstaller --noconfirm --clean --windowed --name QrScreenCaptureAssistant --icon "assets\app.ico" --collect-submodules cv2 --collect-binaries cv2 main.py
  ```

  如仍失败，先用 `--console`（去掉 `--windowed`）重新打包并运行 exe，保留控制台错误信息再排查。

- **PySide6 的 Windows 平台插件**通常由 PyInstaller 自动收集。若出现 “could not find or load the Qt platform plugin windows”，请确认使用的是本 README 的官方 wheel + 固定版本，并在干净虚拟环境中重新打包；不要手动排除 Qt 的 `platforms` 插件。
- **目标电脑缺少 VC++ 运行库**时，可能无法加载 OpenCV/Qt DLL；安装 Microsoft Visual C++ Redistributable 2015–2022 x64 通常可解决。
- **杀毒软件/SmartScreen**：未签名的 PyInstaller 打包文件可能被误报。正式分发时建议对 exe 做代码签名，并使用单目录版降低误报率；不要把此工具伪装成其他软件。
- **单文件 + WeChat 模型**：模型最好放在 exe 旁边的 `models\wechat_qrcode`，或通过环境变量指定。不要仅依赖 PyInstaller 临时解包目录，因为程序是按 exe 所在目录查找模型的。
- PyInstaller 分析阶段可能打印一些 `missing module`（例如 `pwd`、`grp`、`PIL._avif` 等）警告，多数是跨平台或可选功能模块，不影响本项目运行；只有启动时报错才需要处理。
- **测试**：在干净 Windows 虚拟机/另一台机器上验证启动、选择区域、截图保存、可选模型和暂停功能，避免开发机环境掩盖依赖缺失。建议先在命令行窗口运行一次打包后的 exe，观察是否正常启动。
- **发布前检查**：不要打包 `captures/`、历史截图、模型文件（除非你已确认模型许可证允许再分发）或个人信息。

---

## 9. 故障排查

| 现象 | 可能原因 / 处理 |
|---|---|
| `ModuleNotFoundError: No module named 'PySide6'` / `cv2` / `mss` | 依赖装到了另一个 Python。确认 `python` 与 `pip` 属于同一环境，或直接用 `.venv\Scripts\python.exe main.py`。 |
| 点击“开始监控”没有反应 | 需要先选择区域；首次启动时“开始监控”是禁用的。点击“选择监控区域”。 |
| 选择了区域但截图为黑屏 | 某些播放器使用受保护内容/硬件叠层/DRM，Windows 禁止普通截图；锁屏/UAC 安全桌面同理。这不是区域坐标错误。 |
| 截图中区域偏了 / 不是选中的位置 | 显示器排列或缩放被修改，或窗口从旧会话恢复。重新点击“选择监控区域”。混合 DPI 下建议重新框选一次。 |
| 无法识别二维码 | 二维码太小、模糊、被遮挡、对比度低、只出现极短时间，或使用了特殊样式。把区域缩到二维码附近、放大视频画面或提高清晰度；也可尝试可选 WeChatQRCode。 |
| 二维码可以检测到但内容为空 | OpenCV 解码失败，但程序仍保存并显示大图。可以手动用手机扫描；或按第 7 节启用备用解码。 |
| 历史时间在变但记录数不变 | 这是设计行为：同一二维码持续可见/短时重复，只更新时间，不重复新增。 |
| 右侧一直保留旧二维码 | 这是设计行为：二维码消失后保留最近一次结果，方便慢慢扫描。清空历史会同时清空当前显示。 |
| 界面出现滚动条 | 窗口较矮或高 DPI 小屏。滚动即可；可以拉高窗口或拖动分隔条给预览/二维码更多空间。 |
| 截图目录打不开/不可写 | 程序在 `Program Files` 等受保护目录。请把程序放到用户目录，或使用程序回退到的目录（状态栏会提示）。 |
| exe 被杀毒软件拦截 | 未签名的 PyInstaller 程序常见误报。检查源码，自行用本地环境打包；正式分发可做代码签名。 |
| exe 启动报 Qt/platform/cv2 DLL 错误 | 用 `--console` 打包查看完整错误；在干净虚拟环境重建；安装 VC++ 运行库；必要时使用 `--collect-binaries cv2`。 |

如需排查运行期错误，请从 PowerShell 运行 `python main.py`；程序也会将未捕获异常打印到标准错误，并弹窗提示（如果有可用 GUI）。

---

## 10. 验收测试清单

可以按以下步骤自行验收：

1. **框选**：启动后点击“选择监控区域”，在任意显示器拖拽，主界面恢复后能看到区域尺寸和实时预览。
2. **实时捕获**：播放/显示一个二维码。正常硬件环境下，二维码出现后 0.5 秒内右侧应显示放大图。
3. **截图保存**：`captures/` 中出现以本地时间命名的 PNG，尺寸与框选区域一致。
4. **保留结果**：让二维码从画面消失，右侧大图与内容仍保留。
5. **去重**：保持二维码持续显示 5 秒以上，历史记录不应每 150 ms 增加；时间应更新。
6. **历史回看**：再显示另一个二维码，生成第二条记录；点击第一条，右侧切回第一次的大图和内容。
7. **保存/复制**：点击“保存当前二维码图片”能选择本地路径保存；点击“复制二维码内容”后剪贴板内容正确，且不会自动打开任何链接。
8. **暂停/继续**：暂停后 FPS 应降为 0，CPU 明显下降；继续后恢复。
9. **窗口置顶**：勾选后窗口保持在其他窗口上方，取消后恢复普通层级；窗口不会消失。
10. **多显示器/高 DPI**：分别在每个显示器上框选，截取的图像应与所选区域一致；修改缩放后重新选择区域。
11. **无网络**：应用界面无任何上传入口；可通过代码搜索确认没有网络请求，或在断网/防火墙环境下使用。
12. **重开**：关闭并重新打开程序，区域应处于未选择状态；重新框选即可使用，历史记录为新的会话。

---

## 11. 可选的模块化拆分建议

当前刻意交付单文件 `main.py`，便于复制、直接运行和打包。若后续继续开发，建议按以下方式拆分（不要求现在执行）：

```text
qr_screen_assistant/
├─ main.py                      # 仅作为入口：创建 QApplication，加载 MainWindow
├─ config.py                    # 版本、间隔、去重时间、路径等常量
├─ core/
│  ├─ models.py                 # ScreenRegion / NativeMonitor / DetectionPayload / HistoryRecord
│  ├─ qr_detector.py            # QrCodeEngine、WeChat 可选备用、裁剪/放大
│  ├─ capture_worker.py         # MonitorWorker(QThread)，mss + 去重状态机
│  └─ capture_store.py          # captures 目录解析、Pillow PNG 保存、文件名
├─ platform/
│  ├─ windows_monitor.py        # EnumDisplayMonitors、HMONITOR、物理坐标映射
│  └─ dpi.py                    # Qt DIP -> 物理像素映射（便于单元测试）
├─ ui/
│  ├─ main_window.py            # 主窗口、控制状态、历史表格
│  ├─ region_selector.py        # RegionSelector + 每显示器 SelectionOverlay
│  ├─ image_view.py             # 自适应保持比例的 ImageView
│  ├─ theme.py                  # QSS 主题
│  └─ dialogs.py                # 关于/免责声明、确认框
└─ assets/
   └─ app.ico
```

拆分时保持这些边界：

- `core/` 不导入任何 QtWidgets；`QrCodeEngine` 只处理 NumPy/OpenCV。
- 后台线程可以创建和传递 `QImage`，但 `QPixmap`、窗口和控件只能在主线程操作。
- 去重和“新事件判定”留在 `capture_worker` 内，避免多个调用方各自计时导致行为不一致。
- DPI 换算函数纯函数化，使用原生显示器矩形做测试；真实选择层只负责捕获鼠标。
- 先为以下纯逻辑写测试：DPI 映射、2 秒去重、角度点排序、PNG 命名唯一性。

---

## 12. 参考

- Qt for Python：<https://doc.qt.io/qtforpython-6/>
- mss 文档：<https://python-mss.readthedocs.io/>
- OpenCV `QRCodeDetector`：<https://docs.opencv.org/4.x/de/dc3/classcv_1_1QRCodeDetector.html>
- OpenCV WeChatQRCode（可选）：<https://docs.opencv.org/4.x/d5/dae/tutorial_wechat_qrcode.html>
- Pillow：<https://pillow.readthedocs.io/>
- PyInstaller：<https://pyinstaller.org/>

---

**再次提醒：本软件仅用于本地核对你自己有权查看的二维码。不要用它进行未经授权的扫码、登录或规避平台规则。**
