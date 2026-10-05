#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键把本地镜像文件导入成阿里云 ECS 自定义镜像（经 OSS 中转）。

流程:
  1. 目标地域若没有指定的 OSS Bucket 就创建；
  2. 把镜像文件分片上传到 OSS（支持断点续传）；
  3. 调用 ECS ImportImage，用 OSS Bucket/Object 导入，并设置
     启动模式(BIOS/UEFI) 与云盘属性(默认 1 GiB, 该值决定后续 ECS 实例系统盘的下限)；
  4. 轮询镜像状态直到可用；
  5. 导入成功后删除 OSS 里的镜像文件, 避免产生额外存储费用。

参数从脚本同目录的 .env 读取（见 .env.example），命令行参数优先级更高。

注意:
  * OSS Bucket 地域必须与导入镜像的地域一致, 脚本用同一个 region 决定两者。
  * 首次导入需要账号具备服务角色 AliyunECSImageImportDefaultRole, 否则报
    NoSetRoletoECSServiceAccount（在控制台首次导入会自动创建该角色）。
  * ImportImage 直接使用 OssBucket/OssObject, 不需要 OSS 公网链接;
    脚本打印的链接仅供人工核对。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

try:
    import oss2
except ImportError:  # pragma: no cover - 仅在缺依赖时触发
    sys.exit("缺少依赖 oss2, 请先执行: pip install -r requirements.txt")

from dotenv import load_dotenv

from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models
from alibabacloud_ecs20140526.client import Client as EcsClient
from alibabacloud_ecs20140526 import models as ecs_models

try:
    from Tea.exceptions import TeaException
except ImportError:  # pragma: no cover
    TeaException = Exception  # type: ignore

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = util_models.RuntimeOptions(connect_timeout=10000, read_timeout=30000)

# 自定义镜像导入过程中的状态: Creating -> Available, 失败为 Unavailable
IMAGE_STATUS_DONE = "Available"
IMAGE_STATUS_FAILED = {"Unavailable"}

# 阿里云支持的镜像格式（ImportImage 的 Format 取值）
VALID_FORMATS = {"auto", "RAW", "QCOW2", "VHD", "VMDK"}


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> None:
    print(f"[错误] {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def load_config(args: argparse.Namespace) -> dict:
    """读取 .env, 再用命令行参数覆盖。"""
    load_dotenv(os.path.join(HERE, ".env"))
    cfg = {
        "ak": (os.getenv("ALIYUN_ACCESS_KEY_ID") or "").strip(),
        "sk": (os.getenv("ALIYUN_ACCESS_KEY_SECRET") or "").strip(),
        "region": (os.getenv("ALIYUN_REGION_ID") or "").strip(),
        "bucket": (os.getenv("OSS_BUCKET") or "").strip(),
        "endpoint": (os.getenv("OSS_ENDPOINT") or "").strip(),
        "prefix": (os.getenv("IMAGE_OBJECT_PREFIX") or "images/").strip(),
        "boot_mode": (os.getenv("IMAGE_BOOT_MODE") or "BIOS").strip().upper(),
        "platform": (os.getenv("IMAGE_PLATFORM") or "Customized Linux").strip(),
        "architecture": (os.getenv("IMAGE_ARCHITECTURE") or "x86_64").strip(),
        "disk_size": (os.getenv("IMAGE_DISK_SIZE") or "1").strip(),
        "format": (os.getenv("IMAGE_FORMAT") or "auto").strip(),
    }

    # 命令行覆盖
    if args.region:
        cfg["region"] = args.region.strip()
    if args.bucket:
        cfg["bucket"] = args.bucket.strip()
    if args.boot_mode:
        cfg["boot_mode"] = args.boot_mode.strip().upper()
    if args.platform:
        cfg["platform"] = args.platform.strip()
    if args.architecture:
        cfg["architecture"] = args.architecture.strip()
    if args.disk_size is not None:
        cfg["disk_size"] = str(args.disk_size)
    if args.format:
        cfg["format"] = args.format.strip()

    if not cfg["endpoint"] and cfg["region"]:
        cfg["endpoint"] = f"https://oss-{cfg['region']}.aliyuncs.com"

    return cfg


def validate(cfg: dict, image: Path) -> None:
    # bucket 允许为空, 稍后按 地域+随机后缀 自动生成/复用
    missing = [k for k in ("ak", "sk", "region") if not cfg[k]]
    if missing:
        die("缺少必填配置: " + ", ".join(missing) + "（请检查 .env 或命令行参数）")
    if not image.is_file():
        die(f"镜像文件不存在: {image}")
    if image.stat().st_size == 0:
        die(f"镜像文件为空: {image}")
    if cfg["boot_mode"] not in ("BIOS", "UEFI"):
        die(f"boot_mode 只能是 BIOS 或 UEFI, 当前为 {cfg['boot_mode']}")
    if cfg["architecture"] not in ("x86_64", "i386", "arm64"):
        die(f"architecture 只能是 x86_64 / i386 / arm64, 当前为 {cfg['architecture']}")
    if cfg["format"].upper() not in {f.upper() for f in VALID_FORMATS}:
        die(f"format 只能是 {'/'.join(sorted(VALID_FORMATS))}, 当前为 {cfg['format']}")
    try:
        disk = int(cfg["disk_size"])
    except ValueError:
        die(f"disk_size 必须是整数, 当前为 {cfg['disk_size']}")
    if not (1 <= disk <= 2048):
        die("disk_size(云盘属性) 取值范围为 1~2048 GiB")
    # 云盘大小不能小于镜像文件实际大小
    file_gib = image.stat().st_size / (1024 ** 3)
    if disk < file_gib:
        die(f"disk_size={disk} GiB 小于镜像文件实际大小 {file_gib:.2f} GiB, 请调大")


def normalize_image_name(name: str) -> str:
    """镜像名需以字母或中文开头, 且不含空格; 这里做一次兜底规整。"""
    name = name.strip().replace(" ", "-")
    if name and not (name[0].isalpha() or "\u4e00" <= name[0] <= "\u9fff"):
        name = "img-" + name
    return name[:128]


def build_object_key(cfg: dict, image: Path) -> str:
    prefix = cfg["prefix"].lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return f"{prefix}{image.name}"


# --------------------------------------------------------------------------- OSS


def get_bucket(cfg: dict):
    auth = oss2.Auth(cfg["ak"], cfg["sk"])
    return oss2.Bucket(auth, cfg["endpoint"], cfg["bucket"])


def _same_region(location: str, region: str) -> bool:
    """OSS ListBuckets 返回的 Location 可能带 oss- 前缀, 这里做兼容比较。"""
    if not location:
        return True
    loc = location[4:] if location.startswith("oss-") else location
    return loc == region


def resolve_bucket_name(cfg: dict, dry_run: bool):
    """返回 (bucket 名, 是否为本次新建)。

    未设置 OSS_BUCKET 时, 优先复用本账号已有的 imgimport-<地域>-* Bucket;
    没有则生成一个带随机后缀的全局唯一名, 避免手选名字被别人占用。
    只用 OSS 的 ListBuckets(需要 oss:ListBuckets 权限), 不依赖 STS。
    """
    if cfg["bucket"]:
        return cfg["bucket"], False

    prefix = f"imgimport-{cfg['region']}-"
    if dry_run:
        return f"{prefix}<随机后缀>", True

    auth = oss2.Auth(cfg["ak"], cfg["sk"])
    service = oss2.Service(auth, cfg["endpoint"])
    try:
        for b in oss2.BucketIterator(service):
            if b.name.startswith(prefix) and _same_region(b.location, cfg["region"]):
                log(f"复用已有 OSS Bucket: {b.name}")
                return b.name, False
    except oss2.exceptions.OssError as e:
        die(f"列出 OSS Bucket 失败: [{e.code}] {e.message}")

    name = f"{prefix}{uuid.uuid4().hex[:8]}"
    log(f"未设置 OSS_BUCKET, 自动生成唯一名: {name}")
    return name, True


def bucket_taken_hint(cfg: dict, auto_named: bool) -> str:
    """Bucket 名不可用时的可操作提示。"""
    if auto_named:
        return (
            f"Bucket 名 {cfg['bucket']} 暂时不可用。自动生成的名字几乎不会撞名, 通常是"
            f"刚删除过同名 Bucket, OSS 要求等待约 4~8 小时才能重建。\n"
            f"       请稍后重试, 或在 .env 显式设置 OSS_BUCKET 用别的名字。"
        )
    return (
        f"Bucket 名 {cfg['bucket']} 不可用, 可能的原因:\n"
        f"       - 已被其他账号占用(OSS Bucket 名全局唯一, 跨账号、跨地域都不允许重名);\n"
        f"       - 你在其他地域已有同名 Bucket, 而 ImportImage 要求 Bucket 与镜像同地域;\n"
        f"       - 刚删过同名 Bucket, 需等待约 4~8 小时才能重建。\n"
        f"       请用 --bucket 换一个名字, 或清空 .env 的 OSS_BUCKET 让脚本按 地域+随机后缀 自动生成。"
    )


def ensure_bucket(cfg: dict, bucket, auto_named: bool) -> bool:
    """Bucket 不存在则创建。返回是否本次新建。"""
    try:
        bucket.get_bucket_info()
        log(f"OSS Bucket 已存在: {cfg['bucket']} ({cfg['region']})")
        return False
    except oss2.exceptions.NoSuchBucket:
        pass
    except oss2.exceptions.AccessDenied:
        die(
            f"无法访问 OSS Bucket {cfg['bucket']} (403 AccessDenied)。\n"
            f"       {bucket_taken_hint(cfg, auto_named)}\n"
            f"       另外, 若该名字确属本账号, 请确认 AccessKey 具备 oss:GetBucketInfo 权限。"
        )
    except oss2.exceptions.OssError as e:
        die(f"检查 OSS Bucket 失败: [{e.code}] {e.message}")

    log(f"创建 OSS Bucket: {cfg['bucket']} ({cfg['region']})")
    try:
        bucket.create_bucket(oss2.BUCKET_ACL_PRIVATE)
    except oss2.exceptions.OssError as e:
        # BucketAlreadyExists(409) 说明名字被别人/别的地域占用, 不是"我们的桶已存在"
        if e.code == "BucketAlreadyExists":
            die(bucket_taken_hint(cfg, auto_named))
        die(f"创建 OSS Bucket 失败: [{e.code}] {e.message}")
    return True


def upload_image(bucket, key: str, image: Path) -> None:
    total = image.stat().st_size
    state = {"last": -1}

    def on_progress(consumed: int, total_bytes: int) -> None:
        pct = int(consumed * 100 / total_bytes) if total_bytes else 0
        if pct != state["last"]:
            state["last"] = pct
            sys.stderr.write(
                f"\r上传中 {pct:3d}%  "
                f"{consumed / 1048576:.1f}/{total_bytes / 1048576:.1f} MiB"
            )
            sys.stderr.flush()

    log(f"上传镜像: {image.name} ({total / 1048576:.1f} MiB) -> oss://{bucket.bucket_name}/{key}")
    resume_dir = os.path.join(HERE, ".oss_resume")
    os.makedirs(resume_dir, exist_ok=True)
    try:
        oss2.resumable_upload(
            bucket,
            key,
            str(image),
            store=oss2.ResumableStore(root=resume_dir),
            num_threads=4,
            multipart_threshold=100 * 1024 * 1024,
            part_size=50 * 1024 * 1024,
            progress_callback=on_progress,
        )
    except oss2.exceptions.OssError as e:
        sys.stderr.write("\n")
        die(f"上传失败: [{e.code}] {e.message}")
    sys.stderr.write("\n")
    log("上传完成")


def delete_object(bucket, key: str) -> None:
    try:
        bucket.delete_object(key)
        log(f"已删除 OSS 文件: oss://{bucket.bucket_name}/{key}")
    except oss2.exceptions.OssError as e:
        log(f"[警告] 删除 OSS 文件失败: [{e.code}] {e.message}，请手动清理以免产生存储费用")


def delete_bucket_if_empty(cfg: dict, bucket) -> None:
    try:
        names = [o.key for o in oss2.ObjectIterator(bucket, max_keys=1)]
        if names:
            log(f"[警告] Bucket {cfg['bucket']} 非空, 未删除")
            return
        bucket.delete_bucket()
        log(f"已删除 OSS Bucket: {cfg['bucket']}")
    except oss2.exceptions.OssError as e:
        log(f"[警告] 删除 OSS Bucket 失败: [{e.code}] {e.message}")


# --------------------------------------------------------------------------- ECS


def build_ecs_client(cfg: dict) -> EcsClient:
    conf = open_api_models.Config(
        access_key_id=cfg["ak"],
        access_key_secret=cfg["sk"],
    )
    conf.endpoint = f"ecs.{cfg['region']}.aliyuncs.com"
    return EcsClient(conf)


def import_image(ecs: EcsClient, cfg: dict, image_name: str, key: str):
    fmt = cfg["format"]
    mapping = ecs_models.ImportImageRequestDiskDeviceMapping(
        disk_image_size=int(cfg["disk_size"]),
        ossbucket=cfg["bucket"],
        ossobject=key,
        format=None if fmt.lower() == "auto" else fmt.upper(),
    )
    req = ecs_models.ImportImageRequest(
        region_id=cfg["region"],
        image_name=image_name,
        architecture=cfg["architecture"],
        platform=cfg["platform"],
        boot_mode=cfg["boot_mode"],
        client_token=uuid.uuid4().hex,
        disk_device_mapping=[mapping],
    )
    try:
        resp = ecs.import_image_with_options(req, RUNTIME)
    except TeaException as e:
        if e.code == "NoSetRoletoECSServiceAccount":
            die(
                "导入失败: 账号缺少服务角色 AliyunECSImageImportDefaultRole。\n"
                "       请在 RAM 控制台创建该服务角色(控制台首次导入镜像会自动创建), 或使用有权限的账号。"
            )
        die(f"ImportImage 调用失败: [{e.code}] {e.message}")
    return resp.body.image_id, resp.body.task_id


def describe_image(ecs: EcsClient, cfg: dict, image_id: str):
    req = ecs_models.DescribeImagesRequest(region_id=cfg["region"], image_id=image_id)
    resp = ecs.describe_images_with_options(req, RUNTIME)
    images = resp.body.images.image if resp.body.images else None
    return images[0] if images else None


def wait_image_ready(ecs: EcsClient, cfg: dict, image_id: str, timeout: int, interval: int):
    start = time.time()
    last = ""
    while True:
        img = describe_image(ecs, cfg, image_id)
        if img is not None:
            status = img.status or ""
            progress = img.progress or ""
            line = f"镜像状态: {status} {progress}".strip()
            if line != last:
                log(line)
                last = line
            if status == IMAGE_STATUS_DONE:
                return img
            if status in IMAGE_STATUS_FAILED:
                die(f"镜像导入失败, 状态为 {status}")
        if time.time() - start > timeout:
            die(
                f"等待镜像可用超时({timeout}s)。镜像仍在后台导入, 可稍后到控制台查看 ImageId={image_id}；\n"
                f"       本次未删除 OSS 文件, 请确认导入完成后再手动清理。"
            )
        time.sleep(interval)


# --------------------------------------------------------------------------- main


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="一键把本地镜像文件导入成阿里云 ECS 自定义镜像(经 OSS 中转)。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("image", help="本地镜像文件路径(如 debian-custom-13-bios-uefi.qcow2)")
    p.add_argument("--name", help="镜像名称, 默认取文件名(去扩展名)")
    p.add_argument("--description", default="", help="镜像描述")
    p.add_argument("--region", help="地域, 覆盖 .env 的 ALIYUN_REGION_ID")
    p.add_argument("--bucket", help="OSS Bucket, 覆盖 .env 的 OSS_BUCKET")
    p.add_argument("--boot-mode", choices=["BIOS", "UEFI", "bios", "uefi"], help="启动模式")
    p.add_argument("--platform", help="操作系统平台, 如 'Customized Linux' / Debian")
    p.add_argument("--architecture", choices=["x86_64", "i386", "arm64"], help="架构")
    p.add_argument("--disk-size", type=int, help="云盘属性(系统盘 GiB), 决定实例系统盘下限")
    p.add_argument("--format", choices=sorted(VALID_FORMATS), help="镜像格式")
    p.add_argument("--object-key", help="上传到 OSS 的对象 key, 默认 <前缀>/<文件名>")
    p.add_argument("--keep-oss", action="store_true", help="导入完成后保留 OSS 里的镜像文件")
    p.add_argument("--delete-bucket", action="store_true", help="导入完成后, 若本次新建的 Bucket 为空则一并删除")
    p.add_argument("--no-wait", action="store_true", help="只提交导入任务, 不等待完成(此时不删除 OSS 文件)")
    p.add_argument("--timeout", type=int, default=3600, help="等待镜像可用的最长时间(秒)")
    p.add_argument("--interval", type=int, default=15, help="轮询间隔(秒)")
    p.add_argument("--dry-run", action="store_true", help="只打印将要执行的操作, 不实际调用")
    return p.parse_args(argv)


def main() -> None:
    args = parse_args()
    cfg = load_config(args)
    image = Path(args.image).expanduser().resolve()
    validate(cfg, image)

    bucket_name, auto_named = resolve_bucket_name(cfg, args.dry_run)
    cfg["bucket"] = bucket_name

    key = args.object_key or build_object_key(cfg, image)
    image_name = normalize_image_name(args.name or image.stem)
    endpoint = cfg["endpoint"]

    log("=" * 60)
    log("阿里云自定义镜像导入")
    log(f"  镜像文件 : {image}")
    log(f"  地域     : {cfg['region']}")
    log(f"  OSS      : {cfg['bucket']} @ {endpoint}")
    log(f"  对象 key : {key}")
    log(f"  镜像名   : {image_name}")
    log(f"  启动模式 : {cfg['boot_mode']}    平台: {cfg['platform']}    架构: {cfg['architecture']}")
    log(f"  云盘属性 : {cfg['disk_size']} GiB    格式: {cfg['format']}")
    log("=" * 60)

    if args.dry_run:
        log("[dry-run] 将执行: 检查/创建 Bucket -> 上传镜像 -> ImportImage -> 轮询 -> 删除 OSS 文件")
        log("[dry-run] 未执行任何变更。")
        return

    bucket = get_bucket(cfg)
    created_bucket = ensure_bucket(cfg, bucket, auto_named)
    upload_image(bucket, key, image)
    log(f"OSS 链接(仅供核对): oss://{cfg['bucket']}/{key}")
    log(f"                    {endpoint}/{key}")

    ecs = build_ecs_client(cfg)
    log("提交 ImportImage ...")
    image_id, task_id = import_image(ecs, cfg, image_name, key)
    log(f"导入任务已提交: ImageId={image_id}  TaskId={task_id}")

    if args.no_wait:
        log("已按 --no-wait 退出。镜像仍在后台导入, OSS 文件已保留, 完成后请自行清理。")
        return

    log("等待镜像可用(首次导入可能较慢, 请耐心等待) ...")
    img = wait_image_ready(ecs, cfg, image_id, args.timeout, args.interval)
    log(f"镜像已可用: {img.image_id}  名称={img.image_name}  大小={img.size}GiB  "
        f"启动模式={img.boot_mode}")

    if not args.keep_oss:
        delete_object(bucket, key)
    else:
        log("已按 --keep-oss 保留 OSS 文件")

    if args.delete_bucket and created_bucket:
        delete_bucket_if_empty(cfg, bucket)

    log("\n完成。")
    log(f"自定义镜像 ID: {image_id}")
    log(f"镜像名称     : {img.image_name}")
    log(f"地域         : {cfg['region']}")
    log("可在 ECS 控制台 -> 镜像 -> 自定义镜像 中查看, 或用于创建实例。")


if __name__ == "__main__":
    main()
