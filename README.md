用AI写了个自动登录校园网的程序，如果遇到问题，可以在[这里](https://github.com/bb1yd/thisdl-auto-login/issues/new)提交bug报告。
# thisdl-login

macOS 校园网回访认证工具。连接 `Students` 网络或电脑唤醒时，后台程序监听系统事件并尝试使用当次会话自动登录。

## 要求

- macOS 和可运行的 Python 3.9 或更高版本。
- 已通过正常方式在本设备上登录过学校的 `Students` 网络，且学校仍提供二次免认证页面。首次登录、需要密码或其他验证时，请手动登录。
- 不依赖 Homebrew、pip 或第三方 Python 包，也不需要管理员权限。

先检查 Python：

```bash
python3 --version
```

如果系统提示安装苹果的命令行开发工具，则需要先由有权限的人完成安装。`python3` 必须在后续登录时仍然存在；如果 Python 安装路径发生变化，请重新执行 `thisdl-login disable` 和 `thisdl-login enable`。

## 安装

下载代码，在代码所在目录运行：

```bash
python3 ./thisdl-login install
```


## 命令

```text
thisdl-login             显示当前状态
thisdl-login --help      显示帮助
thisdl-login install     安装并启用
thisdl-login enable      启用自动登录
thisdl-login disable     停用自动登录，保留程序
thisdl-login uninstall   停用并卸载
```

停用后重新登录系统仍保持停用。卸载会删除本工具安装的程序和诊断日志。

## 工作方式

后台程序监听 macOS 网络状态和唤醒通知，不做定时轮询。网络事件后立即检测；如果网络尚未准备好，保留两次延迟重试。只有找到学校的 `Students` 回访认证页面时才使用当次会话调用登录接口；不保存账号、密码或 Cookie。

有限大小的诊断日志保存在 `~/Library/Application Support/ThisDLLogin/activity.log`，不记录 Cookie 或带参数的认证地址。程序不会打开学校网站。
