# 参赛镜像 20261101（仓库分卷存档）

GitHub 单文件上限 100 MiB，250.4 MiB 的 `rmtc-team-image.tar.gz` 无法直接入库，
这里按 90 MiB 切成 3 卷。三卷拼回去与 `submission/` 里的原件逐字节一致。

## 还原

```bash
cd 20261101/archive/rmtc-team-image
cat rmtc-team-image.tar.gz.part* > rmtc-team-image.tar.gz
sha256sum rmtc-team-image.tar.gz
```

期望输出：

```
a57b08909696bb8c53ef284327eedfe92e8f52700855c672eec244f4298ddb73  rmtc-team-image.tar.gz
```

与 `20261101/submission/rmtc-team-image.tar.gz.sha256` 相同。

## 分卷大小

| 分卷 | 字节 | 说明 |
| --- | --- | --- |
| `rmtc-team-image.tar.gz.part00` | 94,371,840 | 90 MiB |
| `rmtc-team-image.tar.gz.part01` | 94,371,840 | 90 MiB |
| `rmtc-team-image.tar.gz.part02` | 73,782,393 | 末卷 |
| 合计 | 262,526,073 | 与原件一致 |

## 导入并启动

```bash
gunzip -c rmtc-team-image.tar.gz | docker load
docker images rmtc-team --format '{{.Repository}}:{{.Tag}} {{.ID}}'   # rmtc-team:20261101 21930139bd78
```

正式提交以 `submission/` 下的原件为准，本目录仅作备份与异地还原。
上一轮镜像（`rmtc-team:20261029`）的分卷在 `20261031/archive/rmtc-team-image/`，两轮互不覆盖。
