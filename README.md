# aliyun_import_image

一键把本地镜像文件导入成阿里云 ECS **自定义镜像**：自动建 OSS Bucket、分片上传、调用
`ImportImage` 导入、轮询到可用，并在成功后删除 OSS 里的镜像文件以免产生额外存储费用。

配合 [debian-aliyun-cloud-build](../debian-aliyun-cloud-build) / [alpine-aliyun-cloud-build](../alpine-aliyun-cloud-build)
构建出的 `*.qcow2` 使用，把"手动导入镜像"的七八步控制台操作压缩成一条命令。

## 原理

```
检查/创建 OSS Bucket (同地域, 不存在才建)
  -> 分片上传镜像文件到 OSS (断点续传)
  -> ECS ImportImage (OssBucket + OssObject, 设置 BIOS/UEFI 与云盘属性)
  -> 轮询 DescribeImages 直到 Available
  -> 删除 OSS 里的镜像文件 (默认, 可用 --keep-oss 保留)
```

几点说明：

- **不需要 OSS 公网链接**。`ImportImage` 的参数是 `OssBucket` + `OssObject`，同一账号下
  ECS 直接读取私有 Bucket；脚本打印的 `oss://...` 链接仅供人工核对。
- **地域必须一致**。OSS Bucket 的地域要和导入镜像的地域相同，脚本用同一个 `region`
  同时决定 OSS endpoint 和 `ImportImage` 的 `RegionId`。
- **云盘属性决定实例系统盘下限**。导入时的"云盘属性"（本脚本的 `disk_size`，默认 1 GiB）
  就是镜像的系统盘大小，也是后续用该镜像创建 ECS 实例时系统盘能选的最小值。设成 1 GiB
  就能用最小的 ESSD 系统盘，省成本。

## 前置条件

1. **一个 AccessKey**，权限见同目录 `ram-policy.json`（ECS 导入 + OSS 建桶/上传/删除）。
2. **服务角色 `AliyunECSImageImportDefaultRole`**（重要）。
   - 导入镜像需要 ECS 服务以该角色访问 OSS。**在控制台第一次导入镜像时会自动创建**。
   - 如果账号从没在控制台导入过，纯 API 调用会报 `NoSetRoletoECSServiceAccount`。
     解决办法：先在 ECS 控制台手动导入一次（或用 RAM 控制台创建该服务角色），之后脚本即可正常用。
3. **镜像文件符合阿里云要求**：格式 RAW/QCOW2/VHD，且已按官方"导入镜像必读"做过
   guest 定制（如 cloud-init、网卡名、fstab 等）。

> OSS 会产生少量存储/流量费用。默认导入成功后即删除 OSS 文件；导入过程中（通常几分钟到
> 几十分钟）镜像文件会临时占用 Bucket 空间。

## 安装

```bash
cd aliyun_import_image
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

## 配置

把 `.env.example` 复制为 `.env`，至少填这几项：

```
ALIYUN_ACCESS_KEY_ID=你的AccessKeyId
ALIYUN_ACCESS_KEY_SECRET=你的AccessKeySecret
ALIYUN_REGION_ID=cn-hongkong
OSS_BUCKET=
```

**`OSS_BUCKET` 建议留空**：OSS Bucket 名全局唯一（跨账号、跨地域都不能重名），手选的名字容易被占用。
留空时脚本会复用本账号已有的 `imgimport-<地域>-*` Bucket，没有就新建一个带随机后缀的唯一名，几乎不会撞名；也可以显式填一个名字覆盖。

其余项（`IMAGE_BOOT_MODE`、`IMAGE_PLATFORM`、`IMAGE_DISK_SIZE`、`IMAGE_FORMAT` 等）都有默认值，
也可以用命令行参数临时覆盖。`.env` 已在 `.gitignore` 中，不会被提交。

## 用法

```bash
# 最简：导入，云盘属性用 .env 里的默认值(1 GiB)
python import_image.py debian-custom-13-bios-uefi.qcow2

# 指定名称、启动模式、云盘大小
python import_image.py debian-custom-13-bios-uefi.qcow2 \
    --name debian-13-bios --boot-mode BIOS --disk-size 1

# 只看计划不执行
python import_image.py xxx.qcow2 --dry-run

# 保留 OSS 文件 / 一并删掉本次新建的空 Bucket
python import_image.py xxx.qcow2 --keep-oss --delete-bucket
```

成功后输出 `ImageId`，可在 ECS 控制台"镜像 → 自定义镜像"里看到，或直接用于创建实例。

### 常用参数

| 参数 | 说明 | 默认 |
| :--- | :--- | :--- |
| `image` | 本地镜像文件路径（位置参数） | 必填 |
| `--name` | 镜像名称 | 文件名去扩展名 |
| `--region` | 地域 | `.env` 的 `ALIYUN_REGION_ID` |
| `--bucket` | OSS Bucket | `.env` 的 `OSS_BUCKET` |
| `--boot-mode` | `BIOS` / `UEFI`，**必须与镜像实际引导方式一致** | `.env` 的 `IMAGE_BOOT_MODE` |
| `--platform` | 操作系统平台，自建镜像一般用 `Customized Linux` | `Customized Linux` |
| `--architecture` | `x86_64` / `i386` / `arm64` | `x86_64` |
| `--disk-size` | 云盘属性（系统盘 GiB），决定实例系统盘下限，1~2048 | `1` |
| `--format` | `auto` / `RAW` / `QCOW2` / `VHD` | `auto` |
| `--object-key` | 上传到 OSS 的对象 key | `<前缀>/<文件名>` |
| `--keep-oss` | 导入成功后保留 OSS 文件 | 关闭（即默认删除） |
| `--delete-bucket` | 成功后若本次新建的 Bucket 为空则删除 | 关闭 |
| `--no-wait` | 只提交任务不等待（此时不删 OSS 文件） | 关闭 |
| `--timeout` | 等待镜像可用的最长时间（秒） | `3600` |
| `--dry-run` | 只打印计划，不调用任何接口 | 关闭 |

## 清理行为

- 默认：**镜像变为 Available 后才删除 OSS 文件**（导入过程中文件必须先存在）。
- 导入失败或超时：**保留 OSS 文件**，方便重试；请自行确认后清理。
- `--no-wait`：无法判断导入是否完成，因此**不删除**，请稍后手动清理。
- `--keep-oss`：始终保留。

## 常见错误

| 报错 | 原因 / 处理 |
| :--- | :--- |
| `NoSetRoletoECSServiceAccount` | 账号缺服务角色 `AliyunECSImageImportDefaultRole`，见"前置条件 2" |
| `BucketAlreadyExists`（409，建桶时） | Bucket 名不可用：被其他账号占用 / 你在别的地域已有同名 / 刚删过同名（需等约 4~8 小时）。脚本会给出提示，换名或留空 `OSS_BUCKET` 自动生成即可 |
| `AccessDenied`（403，检查 Bucket 时） | 该 Bucket 名属于其他账号，或 AccessKey 缺 `oss:GetBucketInfo`；参考 `ram-policy.json` |
| `Forbidden` / `AccessDenied`（上传/导入时） | AccessKey 缺 `oss:PutObject` 等权限，参考 `ram-policy.json` |
| `InvalidImageName` | 镜像名需以字母或中文开头、2~128 字符；脚本已做兜底规整 |
| `InvalidParameter`（DiskImageSize） | 云盘属性必须 1~2048 GiB，且不小于镜像文件实际大小 |
| 实例启动 "Booting from Hard Disk..." | 导入时的 `--boot-mode` 与镜像不一致，重新按正确模式导入 |
