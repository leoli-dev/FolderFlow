# 公开仓库与发布

## 仓库内容

仓库跟踪源码、测试、构建脚本、文档与许可证。
`data/`、`reports/`、虚拟环境、构建目录、EXE、ZIP 和证书在 `.gitignore` 中。
当前发行包从白名单生成，不包含个人来源、目标、网络地址、任务或清单。
二进制作为 Release 附件发布，不提交到 Git 历史。

## 创建公共远程仓库

本项目可先在本地审阅。下面命令仅供维护者决定发布后手动执行；将 OWNER 替换为你的 GitHub 用户/组织。
需要预先安装并登录 GitHub CLI。

```powershell
gh repo create OWNER/FolderFlow --public --source=. --remote=origin --push
```

已有远程仓库时：

```powershell
git remote add origin https://github.com/OWNER/FolderFlow.git
git push -u origin main
```

## 构建和发布版本

1. 更新包版本、项目版本、manifest 版本及对应 `docs/release-notes-vX.Y.Z.md`。
2. 使用默认 `scripts/build.ps1` 完成测试与绿色包构建。
3. 从公开 ZIP 解压启动，确认是空任务列表；使用临时目录验证分析、复制、校验。
4. 提交源码与文档，打版本标签。
5. 将 ZIP 和 `SHA256SUMS.txt` 作为 Release 附件上传。

```powershell
git tag v2.0.0
git push origin v2.0.0
gh release create v2.0.0 .\dist\FolderFlow-2.0.0-windows-x64-portable.zip .\dist\SHA256SUMS.txt --title "FolderFlow 2.0.0" --notes-file .\docs\release-notes-v2.0.0.md
```

CI 在 push、pull request 和手动触发时测试并构建可下载 artifacts，权限仅为读取源码，
不会自动创建 GitHub Release。Actions 使用固定提交版本。

## 本地数据与升级

不要上传现用软件目录中的个人状态或报告。公开包中没有 `data/`、`reports/` 和备份元数据。
现有用户升级时保留自己的 `data/`，替换程序文件和附带文档；不要同时用两个版本执行同一目标。

项目源码使用 MIT；运行库许可随包附带在 `third_party/`。
EXE 当前没有代码签名；需要签名时由维护者在发布流程中自行配置，签名私钥不进入仓库。
