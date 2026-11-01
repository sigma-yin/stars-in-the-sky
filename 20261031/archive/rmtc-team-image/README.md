# 参赛镜像（仓库分卷存档）

GitHub 单文件上限 100 MB，251 MB 的 `rmtc-team-image.tar.gz` 无法直接入库，
这里按 90 MiB 切成 3 卷存储。三卷拼回去与 `submission/` 里的原件逐字节一致。

还原：

```bash
cd archive/rmtc-team-image
cat rmtc-team-image.tar.gz.part* > rmtc-team-image.tar.gz
sha256sum rmtc-team-image.tar.gz
```

期望输出 `ad3dc3b87b03e97bddce8cade36f90c5dd7e73e888c521025d5490a787e2aaab`，
与 `submission/rmtc-team-image.tar.gz.sha256` 相同。

导入并启动：

```bash
gunzip -c rmtc-team-image.tar.gz | docker load
docker images rmtc-team --format '{{.Repository}}:{{.Tag}} {{.ID}}'   # rmtc-team:20261029 068321cfe973
```

正式提交以 `submission/` 下的原件为准，本目录仅作备份与异地还原。
