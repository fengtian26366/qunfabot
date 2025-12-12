# ============================================================
# BG678 群发机器人（Webhook 稳定版 / Railway 适用 / PTB v21.7+）
# 功能：
# - /start 菜单（仅管理员）
# - /id 查看自己的 Telegram 数字ID（任何人可用）
# - 群内：/register 绑定群，/unregister 解绑群（仅管理员）
# - 私聊：群管理（查看/删除/清空）
# - 私聊：立即发送（选群 -> 选择删除分钟 -> 按钮配置 -> 发内容）
# - 私聊：定时发送（选群 -> 输入时间 -> 删除分钟 -> 按钮配置 -> 发内容）
# - 私聊：每日循环（选群 -> 输入时间 -> 删除分钟 -> 按钮配置 -> 发内容）
# - 我的帖子：查看/编辑内容/删除/启停（按钮也会随任务发出）
# - 重启恢复 schedule/daily 任务（从 posts.json）
# ============================================================

import os
import re
import json
import uuid
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone, time as dtime
from typing import Optional, Dict, List, Any, Set

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    CopyTextButton,  # PTB v21.7+
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================
# 环境变量（Railway Variables 里填）
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").strip()  # https://xxxx.up.railway.app
PORT = int(os.getenv("PORT", "8080"))

# 时区：默认柬埔寨 +7；要改成 +8 就设置 TZ_OFFSET=8
TZ_OFFSET = int(os.getenv("TZ_OFFSET", "7"))
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))

def parse_admin_ids() -> Set[int]:
    raw = os.getenv("ADMIN_IDS", "").strip()
    if not raw:
        return set()
    return {int(x) for x in re.split(r"[,\s]+", raw) if x}

ADMIN_IDS = parse_admin_ids()

# =========================
# 日志
# =========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("BG678WebhookBot")

# =========================
# 数据文件（跟脚本同目录）
# =========================
BASE_DIR = Path(__file__).resolve().parent
GROUPS_FILE = BASE_DIR / "groups.json"
POSTS_FILE = BASE_DIR / "posts.json"

# =========================
# 状态机 Key
# =========================
MODE = "mode"
STEP = "step"
TEMP = "temp"
SELECTED_GROUPS = "selected_groups"
EDIT_POST_ID = "edit_post_id"
BUTTONS = "buttons"  # ✅ 新增：保存按钮配置

M_IMMEDIATE = "immediate"
M_SCHEDULE = "schedule"
M_DAILY = "daily"
M_EDIT = "edit"

S_CHOOSE_GROUPS = "choose_groups"
S_ASK_SEND_TIME = "ask_send_time"
S_ASK_DELETE_MIN = "ask_delete_min"

# ✅ 新增按钮配置步骤
S_ASK_BUTTON_ENABLE = "ask_button_enable"   # 1不加 2加
S_ASK_COPY_BTN_TEXT = "ask_copy_btn_text"
S_ASK_COPY_VALUE = "ask_copy_value"
S_ASK_URL_BTN_TEXT = "ask_url_btn_text"
S_ASK_URL_VALUE = "ask_url_value"

S_AWAIT_CONTENT = "await_content"

# =========================
# 菜单
# =========================
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📤 发送帖子", "📝 我的帖子"],
        ["🧩 群管理", "🧪 Debug"],
    ],
    resize_keyboard=True
)

SEND_MENU = ReplyKeyboardMarkup(
    [
        ["🚀 立即发送"],
        ["⏰ 定时发送"],
        ["🔁 每日循环发送"],
        ["⬅️ 返回菜单"],
    ],
    resize_keyboard=True
)

# =========================
# 工具函数
# =========================
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def now_local() -> datetime:
    return datetime.now(tz=LOCAL_TZ)

def gen_id() -> str:
    return uuid.uuid4().hex[:8]

def load_groups() -> Dict[str, str]:
    if GROUPS_FILE.exists():
        try:
            return json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"groups.json 解析失败：{e}")
            return {}
    return {}

def save_groups(data: Dict[str, str]):
    GROUPS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def load_posts() -> List[Dict[str, Any]]:
    if POSTS_FILE.exists():
        try:
            return json.loads(POSTS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"posts.json 解析失败：{e}")
            return []
    return []

def save_posts(posts: List[Dict[str, Any]]):
    POSTS_FILE.write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")

def content_from_message(msg) -> Dict[str, Any]:
    if msg.photo:
        return {"type": "photo", "photo_id": msg.photo[-1].file_id, "caption": msg.caption or ""}
    return {"type": "text", "text": msg.text or msg.caption or ""}

def parse_dt_full(text: str) -> Optional[datetime]:
    """支持：YYYY/MM/DD HH:MM 或 YYYY/MM/DD HH:MM:SS（LOCAL_TZ）"""
    if not text:
        return None
    t = text.strip().replace("：", ":")
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            dt = datetime.strptime(t, fmt)
            return dt.replace(tzinfo=LOCAL_TZ)
        except Exception:
            pass
    return None

def parse_time_flexible(text: str) -> Optional[dtime]:
    # 支持：20:30 / 20点30 / 9点 / 21
    if not text:
        return None
    t = text.strip()
    t = t.replace("：", ":").replace("点", ":").replace("分", "").replace(" ", "")
    parts = [p for p in t.split(":") if p != ""]
    try:
        if len(parts) == 1 and parts[0].isdigit():
            return dtime(hour=int(parts[0]), minute=0, second=0)
        if len(parts) == 2:
            return dtime(hour=int(parts[0]), minute=int(parts[1]), second=0)
        if len(parts) == 3:
            return dtime(hour=int(parts[0]), minute=int(parts[1]), second=int(parts[2]))
    except Exception:
        return None
    return None

def today_dt(tm: dtime) -> datetime:
    n = now_local()
    return datetime(n.year, n.month, n.day, tm.hour, tm.minute, tm.second, tzinfo=LOCAL_TZ)

def get_post(posts: List[Dict[str, Any]], post_id: str) -> Optional[Dict[str, Any]]:
    return next((x for x in posts if x.get("id") == post_id), None)

def remove_jobs_by_name(job_queue, name: str):
    if not job_queue or not name:
        return
    for j in job_queue.get_jobs_by_name(name):
        j.schedule_removal()

def fmt_post(p: Dict[str, Any]) -> str:
    s = f"🆔 ID: {p.get('id')}\n📌 类型: {p.get('type')}\n"
    s += f"👥 群数: {len(p.get('groups', []))}\n"
    s += f"🟢 状态: {'启用' if p.get('enabled', True) else '停用'}\n"
    if p.get("type") == "schedule":
        s += f"⏰ 发送时间: {p.get('send_time')}\n"
        s += f"🗑 自动删除: {int(p.get('delete_minutes', 0))} 分钟\n"
    if p.get("type") == "daily":
        s += f"🔁 每日时间: {p.get('daily_time')}\n"
        s += f"🗑 自动删除: {int(p.get('delete_minutes', 0))} 分钟\n"
    b = p.get("buttons") or {}
    if b:
        s += "🔘 按钮: 已配置\n"
    return s

def build_group_keyboard(prefix: str, selected: Set[str]) -> InlineKeyboardMarkup:
    groups = load_groups()
    kb, row = [], []
    for cid, title in groups.items():
        mark = "✅" if cid in selected else "☑"
        row.append(InlineKeyboardButton(f"{mark} {title}", callback_data=f"{prefix}_tg:{cid}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([
        InlineKeyboardButton("✅ 完成选择", callback_data=f"{prefix}_done"),
        InlineKeyboardButton("❌ 取消", callback_data=f"{prefix}_cancel"),
    ])
    return InlineKeyboardMarkup(kb)

def ensure_job_queue(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return getattr(context, "job_queue", None) is not None

# ============================================================
# ✅ 模块化：按钮构建 & 发送（你要的“2”）
# ============================================================
def is_valid_url(u: str) -> bool:
    u = (u or "").strip()
    return u.startswith("https://") or u.startswith("http://")

def build_buttons(buttons: Optional[Dict[str, Any]]) -> Optional[InlineKeyboardMarkup]:
    """
    buttons 结构：
    {
      "copy": {"text": "...", "value": "..."},
      "url":  {"text": "...", "url": "https://..."}
    }
    """
    if not buttons:
        return None

    row = []

    c = buttons.get("copy")
    if isinstance(c, dict):
        text = (c.get("text") or "").strip()
        value = (c.get("value") or "").strip()
        if text and value and len(value) <= 256:
            row.append(InlineKeyboardButton(text, copy_text=CopyTextButton(value)))

    u = buttons.get("url")
    if isinstance(u, dict):
        text = (u.get("text") or "").strip()
        url = (u.get("url") or "").strip()
        if text and url and is_valid_url(url):
            row.append(InlineKeyboardButton(text, url=url))

    if not row:
        return None
    return InlineKeyboardMarkup([row])

async def send_content(context: ContextTypes.DEFAULT_TYPE, chat_id: int, content: Dict[str, Any], buttons: Optional[Dict[str, Any]] = None):
    rm = build_buttons(buttons)

    if content.get("type") == "photo":
        return await context.bot.send_photo(
            chat_id=chat_id,
            photo=content.get("photo_id"),
            caption=content.get("caption", "") or "",
            reply_markup=rm
        )
    else:
        return await context.bot.send_message(
            chat_id=chat_id,
            text=content.get("text", "") or "",
            reply_markup=rm
        )

# =========================
# 基础命令
# =========================
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(f"你的 Telegram ID：{user.id}")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text(f"⛔ 无权限。\n你的ID：{user.id}\n请让管理员把你的ID加入 ADMIN_IDS 环境变量。")
        return
    await update.message.reply_text(
        "✅ BG678 群发机器人（Webhook 稳定版）已启动\n\n"
        "群内绑定：/register\n群内解绑：/unregister\n私聊群管理：/managegroups\n\n"
        "也可以直接用下方菜单按钮。",
        reply_markup=MAIN_KEYBOARD
    )

async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text(f"⛔ 无权限。你的ID：{user.id}")
        return
    g = load_groups()
    p = load_posts()
    jq = "OK" if getattr(context, "job_queue", None) is not None else "MISSING"
    await update.message.reply_text(
        "🧪 Debug\n"
        f"BASE_DIR: {BASE_DIR}\n"
        f"groups_file: {GROUPS_FILE}\n"
        f"posts_file: {POSTS_FILE}\n"
        f"群数量: {len(g)}\n"
        f"任务数量: {len(p)}\n"
        f"job_queue: {jq}\n"
        f"TZ_OFFSET: {TZ_OFFSET}\n"
    )

# =========================
# 绑定 / 解绑
# =========================
async def register_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text(f"⛔ 无权限。你的ID：{user.id}")
        return

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("请在群内使用 /register")
        return

    groups = load_groups()
    groups[str(chat.id)] = chat.title or f"group_{chat.id}"
    save_groups(groups)
    await update.message.reply_text(f"✅ 已绑定群：{groups[str(chat.id)]}")

async def unregister_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text(f"⛔ 无权限。你的ID：{user.id}")
        return

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("请在群内使用 /unregister")
        return

    groups = load_groups()
    cid = str(chat.id)
    if cid in groups:
        title = groups.pop(cid)
        save_groups(groups)
        await update.message.reply_text(f"❌ 已解绑群：{title}")
    else:
        await update.message.reply_text("该群尚未绑定，无需解绑。")

# =========================
# 私聊群管理
# =========================
async def managegroups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text(f"⛔ 无权限。你的ID：{user.id}")
        return

    groups = load_groups()
    if not groups:
        await update.message.reply_text("📭 当前没有任何绑定群", reply_markup=MAIN_KEYBOARD)
        return

    text = "📋 已绑定群：\n\n"
    kb = []
    for cid, title in groups.items():
        text += f"• {title} ({cid})\n"
        kb.append([InlineKeyboardButton(f"❌ 解绑 {title}", callback_data=f"mg_del:{cid}")])
    kb.append([InlineKeyboardButton("🧹 清空全部", callback_data="mg_clear")])

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def managegroups_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    if not is_admin(user.id):
        await q.answer("无权限")
        return

    data = q.data
    groups = load_groups()

    if data.startswith("mg_del:"):
        cid = data.split(":", 1)[1]
        groups.pop(cid, None)
        save_groups(groups)
        await q.answer("已解绑")
        await q.message.delete()
        return

    if data == "mg_clear":
        save_groups({})
        await q.answer("已清空")
        await q.message.delete()
        return

# =========================
# 发送菜单
# =========================
async def menu_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("请选择发帖方式：", reply_markup=SEND_MENU)

# ============================================================
# ✅ 新增：通用按钮配置流程（立即/定时/每日 共用）
# ============================================================
async def ask_button_enable(msg, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[STEP] = S_ASK_BUTTON_ENABLE
    await msg.reply_text(
        "是否添加按钮？\n"
        "1️⃣ 不添加\n"
        "2️⃣ 添加按钮（复制 + 跳转）"
    )

async def handle_button_flow(msg, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    返回 True 表示已处理（不继续往下）
    返回 False 表示不在按钮流程，交给别的逻辑
    """
    step = context.user_data.get(STEP)
    text = (msg.text or "").strip()

    if step == S_ASK_BUTTON_ENABLE:
        if text == "1":
            context.user_data[BUTTONS] = None
            context.user_data[STEP] = S_AWAIT_CONTENT
            await msg.reply_text("请发送要群发的内容（文字或图片+文字）：")
            return True
        if text == "2":
            context.user_data[BUTTONS] = {"copy": {}, "url": {}}
            context.user_data[STEP] = S_ASK_COPY_BTN_TEXT
            await msg.reply_text("请输入【复制按钮名称】（例如：Redeem / Claim / Use Code）")
            return True

        await msg.reply_text("请输入 1 或 2")
        return True

    if step == S_ASK_COPY_BTN_TEXT:
        if not text:
            await msg.reply_text("按钮名称不能为空，请重新输入")
            return True
        context.user_data[BUTTONS]["copy"]["text"] = text
        context.user_data[STEP] = S_ASK_COPY_VALUE
        await msg.reply_text("请输入【复制内容】（例如：LT79KYN9A）")
        return True

    if step == S_ASK_COPY_VALUE:
        if not text:
            await msg.reply_text("复制内容不能为空，请重新输入")
            return True
        if len(text) > 256:
            await msg.reply_text("复制内容太长（>256），请缩短后再输入")
            return True
        context.user_data[BUTTONS]["copy"]["value"] = text
        context.user_data[STEP] = S_ASK_URL_BTN_TEXT
        await msg.reply_text("请输入【跳转按钮名称】（例如：Contact Support / Redeem Offer）")
        return True

    if step == S_ASK_URL_BTN_TEXT:
        if not text:
            await msg.reply_text("按钮名称不能为空，请重新输入")
            return True
        context.user_data[BUTTONS]["url"]["text"] = text
        context.user_data[STEP] = S_ASK_URL_VALUE
        await msg.reply_text("请输入【跳转网址】（必须 http/https 开头，例如：https://t.me/BG678SeniorAdvisor）")
        return True

    if step == S_ASK_URL_VALUE:
        if not is_valid_url(text):
            await msg.reply_text("网址格式不对，请输入 http:// 或 https:// 开头的链接")
            return True
        context.user_data[BUTTONS]["url"]["url"] = text
        context.user_data[STEP] = S_AWAIT_CONTENT
        await msg.reply_text("✅ 按钮已配置完成。请发送要群发的内容（文字或图片+文字）：")
        return True

    return False

# =========================
# 立即发送
# =========================
async def immediate_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not load_groups():
        await update.message.reply_text("❗ 没有绑定群，请先在群里 /register", reply_markup=MAIN_KEYBOARD)
        return

    context.user_data.clear()
    context.user_data[MODE] = M_IMMEDIATE
    context.user_data[STEP] = S_CHOOSE_GROUPS
    context.user_data[SELECTED_GROUPS] = set()
    context.user_data[TEMP] = {}
    context.user_data[BUTTONS] = None

    await update.message.reply_text("请选择要发送的群：", reply_markup=build_group_keyboard("im", set()))

async def immediate_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("无权限")
        return

    if context.user_data.get(MODE) != M_IMMEDIATE:
        await q.answer("当前不在立即发送流程")
        return

    data = q.data
    selected: Set[str] = set(context.user_data.get(SELECTED_GROUPS, set()))

    if data.startswith("im_tg:"):
        cid = data.split(":", 1)[1]
        if cid in selected:
            selected.remove(cid)
        else:
            selected.add(cid)
        context.user_data[SELECTED_GROUPS] = selected
        await q.edit_message_reply_markup(build_group_keyboard("im", selected))
        return

    if data == "im_cancel":
        context.user_data.clear()
        await q.answer("已取消")
        await q.message.reply_text("已取消。", reply_markup=MAIN_KEYBOARD)
        try:
            await q.message.delete()
        except Exception:
            pass
        return

    if data == "im_done":
        if not selected:
            await q.answer("请至少选择一个群")
            return
        context.user_data[STEP] = S_ASK_DELETE_MIN
        await q.answer("请输入删除分钟")
        await q.message.reply_text("若需自动删除，请输入【发送后多少分钟删除】（数字），不删输入 0", reply_markup=ReplyKeyboardRemove())
        try:
            await q.message.delete()
        except Exception:
            pass
        return

async def immediate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get(MODE) != M_IMMEDIATE:
        return

    msg = update.message

    # 先处理按钮流程（如果在流程里）
    if await handle_button_flow(msg, context):
        return

    step = context.user_data.get(STEP)
    text = (msg.text or "").strip()

    if step == S_ASK_DELETE_MIN:
        if not text.isdigit():
            await msg.reply_text("❗ 请输入数字分钟或 0")
            return
        context.user_data[TEMP]["delete_minutes"] = int(text)
        await ask_button_enable(msg, context)
        return

    if step == S_AWAIT_CONTENT:
        groups_map = load_groups()
        selected: Set[str] = set(context.user_data.get(SELECTED_GROUPS, set()))
        selected = {cid for cid in selected if cid in groups_map}
        if not selected:
            await msg.reply_text("❗ 当前可发送群为 0。已取消。", reply_markup=MAIN_KEYBOARD)
            context.user_data.clear()
            return

        content = content_from_message(msg)
        buttons = context.user_data.get(BUTTONS)
        delete_minutes = int(context.user_data.get(TEMP, {}).get("delete_minutes", 0))

        sent_msgs = []
        sent, failed = 0, 0
        reasons = []

        for cid in selected:
            try:
                m = await send_content(context, int(cid), content, buttons=buttons)
                sent_msgs.append({"chat_id": cid, "message_id": m.message_id})
                sent += 1
            except Exception as e:
                failed += 1
                reasons.append(f"{groups_map.get(cid)} ({cid}) -> {e}")
                logger.error(f"[立即发送失败] chat={cid} err={e}")

        # 立即发送也支持自动删除（如果安装了 job_queue）
        if delete_minutes > 0 and sent_msgs and ensure_job_queue(context):
            context.job_queue.run_once(
                delete_messages_job,
                when=delete_minutes * 60,
                data={"messages": sent_msgs}
            )

        report = f"🎉 立即发送完成：成功 {sent} 群，失败 {failed} 群。"
        if reasons:
            report += "\n\n❌ 失败原因：\n" + "\n".join(reasons[:10])

        await msg.reply_text(report, reply_markup=MAIN_KEYBOARD)
        context.user_data.clear()
        return

# =========================
# 定时发送（一次性）
# =========================
async def schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not load_groups():
        await update.message.reply_text("❗ 没有绑定群，请先在群里 /register", reply_markup=MAIN_KEYBOARD)
        return

    context.user_data.clear()
    context.user_data[MODE] = M_SCHEDULE
    context.user_data[STEP] = S_CHOOSE_GROUPS
    context.user_data[SELECTED_GROUPS] = set()
    context.user_data[TEMP] = {}
    context.user_data[BUTTONS] = None

    await update.message.reply_text("请选择要定时发送的群：", reply_markup=build_group_keyboard("sc", set()))

async def schedule_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("无权限")
        return
    if context.user_data.get(MODE) != M_SCHEDULE:
        await q.answer("当前不在定时流程")
        return

    data = q.data
    selected: Set[str] = set(context.user_data.get(SELECTED_GROUPS, set()))

    if data.startswith("sc_tg:"):
        cid = data.split(":", 1)[1]
        if cid in selected:
            selected.remove(cid)
        else:
            selected.add(cid)
        context.user_data[SELECTED_GROUPS] = selected
        await q.edit_message_reply_markup(build_group_keyboard("sc", selected))
        return

    if data == "sc_cancel":
        context.user_data.clear()
        await q.answer("已取消")
        await q.message.reply_text("已取消。", reply_markup=MAIN_KEYBOARD)
        try:
            await q.message.delete()
        except Exception:
            pass
        return

    if data == "sc_done":
        if not selected:
            await q.answer("请至少选择一个群")
            return
        context.user_data[STEP] = S_ASK_SEND_TIME
        await q.answer("请发送时间")
        await q.message.reply_text(
            "请发送【发送时间】：\n"
            "✅ YYYY/MM/DD HH:MM  或  YYYY/MM/DD HH:MM:SS\n"
            "✅ 或：20:30 / 20点30 / 9点（默认今天）",
            reply_markup=ReplyKeyboardRemove()
        )
        try:
            await q.message.delete()
        except Exception:
            pass
        return

async def schedule_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get(MODE) != M_SCHEDULE:
        return

    msg = update.message

    # 按钮流程优先
    if await handle_button_flow(msg, context):
        return

    step = context.user_data.get(STEP)
    text = (msg.text or "").strip()

    if step == S_ASK_SEND_TIME:
        dt = parse_dt_full(text)
        if not dt:
            tm = parse_time_flexible(text)
            if tm:
                dt = today_dt(tm)
        if not dt:
            await msg.reply_text("❗ 时间格式错误，请重新输入：2025/12/12 20:30 或 20点30")
            return
        if dt <= now_local():
            await msg.reply_text("❗ 发送时间必须晚于当前时间，请重新输入")
            return

        context.user_data[TEMP]["send_time"] = dt.isoformat()
        context.user_data[STEP] = S_ASK_DELETE_MIN
        await msg.reply_text("若需自动删除，请输入【发送后多少分钟删除】（数字），不删输入 0")
        return

    if step == S_ASK_DELETE_MIN:
        if not text.isdigit():
            await msg.reply_text("❗ 请输入数字分钟或 0")
            return
        context.user_data[TEMP]["delete_minutes"] = int(text)
        await ask_button_enable(msg, context)
        return

    if step == S_AWAIT_CONTENT:
        if not ensure_job_queue(context):
            await msg.reply_text("❗ 当前环境缺少 JobQueue 依赖（job_queue=None）。请按 requirements.txt 安装 PTB job-queue。", reply_markup=MAIN_KEYBOARD)
            context.user_data.clear()
            return

        groups_map = load_groups()
        selected: Set[str] = set(context.user_data.get(SELECTED_GROUPS, set()))
        selected = {cid for cid in selected if cid in groups_map}
        if not selected:
            await msg.reply_text("❗ 当前选择群为空，已取消。", reply_markup=MAIN_KEYBOARD)
            context.user_data.clear()
            return

        post_id = gen_id()
        send_time = context.user_data[TEMP]["send_time"]
        delete_minutes = int(context.user_data[TEMP].get("delete_minutes", 0))
        buttons = context.user_data.get(BUTTONS)
        content = content_from_message(msg)

        job_name = f"schedule_{post_id}"

        posts = load_posts()
        posts.append({
            "id": post_id,
            "type": "schedule",
            "groups": list(selected),
            "send_time": send_time,
            "delete_minutes": delete_minutes,
            "content": content,
            "buttons": buttons,
            "enabled": True,
            "job_name": job_name,
        })
        save_posts(posts)

        dt = datetime.fromisoformat(send_time)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        delay = max(1, int((dt - now_local()).total_seconds()))

        context.job_queue.run_once(
            schedule_execute_job,
            when=delay,
            data={"post_id": post_id},
            name=job_name
        )

        await msg.reply_text(f"⏰ 定时任务已创建（ID: {post_id}）", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear()
        return

async def schedule_execute_job(context: ContextTypes.DEFAULT_TYPE):
    post_id = context.job.data.get("post_id")
    posts = load_posts()
    post = get_post(posts, post_id)
    if not post or not post.get("enabled", True):
        return

    groups = post.get("groups", [])
    content = post.get("content", {})
    buttons = post.get("buttons")
    delete_minutes = int(post.get("delete_minutes", 0))

    sent_msgs = []
    for cid in groups:
        try:
            m = await send_content(context, int(cid), content, buttons=buttons)
            sent_msgs.append({"chat_id": cid, "message_id": m.message_id})
        except Exception as e:
            logger.error(f"[定时发送失败] post={post_id} chat={cid} err={e}")

    if delete_minutes > 0 and sent_msgs and getattr(context, "job_queue", None) is not None:
        context.job_queue.run_once(
            delete_messages_job,
            when=delete_minutes * 60,
            data={"messages": sent_msgs}
        )

async def delete_messages_job(context: ContextTypes.DEFAULT_TYPE):
    msgs = context.job.data.get("messages", [])
    for item in msgs:
        try:
            await context.bot.delete_message(
                chat_id=int(item["chat_id"]),
                message_id=int(item["message_id"])
            )
        except Exception as e:
            logger.error(f"[删除失败] chat={item.get('chat_id')} msg={item.get('message_id')} err={e}")

# =========================
# 每日循环
# =========================
async def daily_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not load_groups():
        await update.message.reply_text("❗ 没有绑定群，请先在群里 /register", reply_markup=MAIN_KEYBOARD)
        return

    context.user_data.clear()
    context.user_data[MODE] = M_DAILY
    context.user_data[STEP] = S_CHOOSE_GROUPS
    context.user_data[SELECTED_GROUPS] = set()
    context.user_data[TEMP] = {}
    context.user_data[BUTTONS] = None

    await update.message.reply_text("请选择要每日循环发送的群：", reply_markup=build_group_keyboard("dy", set()))

async def daily_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("无权限")
        return
    if context.user_data.get(MODE) != M_DAILY:
        await q.answer("当前不在每日流程")
        return

    data = q.data
    selected: Set[str] = set(context.user_data.get(SELECTED_GROUPS, set()))

    if data.startswith("dy_tg:"):
        cid = data.split(":", 1)[1]
        if cid in selected:
            selected.remove(cid)
        else:
            selected.add(cid)
        context.user_data[SELECTED_GROUPS] = selected
        await q.edit_message_reply_markup(build_group_keyboard("dy", selected))
        return

    if data == "dy_cancel":
        context.user_data.clear()
        await q.answer("已取消")
        await q.message.reply_text("已取消。", reply_markup=MAIN_KEYBOARD)
        try:
            await q.message.delete()
        except Exception:
            pass
        return

    if data == "dy_done":
        if not selected:
            await q.answer("请至少选择一个群")
            return
        context.user_data[STEP] = S_ASK_SEND_TIME
        await q.answer("请输入每日时间")
        await q.message.reply_text("请输入每日发送时间：20:30 / 20点30 / 9点 等", reply_markup=ReplyKeyboardRemove())
        try:
            await q.message.delete()
        except Exception:
            pass
        return

async def daily_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get(MODE) != M_DAILY:
        return

    msg = update.message

    # 按钮流程优先
    if await handle_button_flow(msg, context):
        return

    step = context.user_data.get(STEP)
    text = (msg.text or "").strip()

    if step == S_ASK_SEND_TIME:
        tm = parse_time_flexible(text)
        if not tm:
            await msg.reply_text("❗ 时间格式错误，请重新输入：20:30 / 20点30 / 9点")
            return

        context.user_data[TEMP]["daily_time"] = text
        context.user_data[STEP] = S_ASK_DELETE_MIN
        await msg.reply_text("若需自动删除，请输入【发送后多少分钟删除】（数字），不删输入 0")
        return

    if step == S_ASK_DELETE_MIN:
        if not text.isdigit():
            await msg.reply_text("❗ 请输入数字分钟或 0")
            return
        context.user_data[TEMP]["delete_minutes"] = int(text)
        await ask_button_enable(msg, context)
        return

    if step == S_AWAIT_CONTENT:
        if not ensure_job_queue(context):
            await msg.reply_text("❗ 当前环境缺少 JobQueue 依赖（job_queue=None）。请按 requirements.txt 安装 PTB job-queue。", reply_markup=MAIN_KEYBOARD)
            context.user_data.clear()
            return

        groups_map = load_groups()
        selected: Set[str] = set(context.user_data.get(SELECTED_GROUPS, set()))
        selected = {cid for cid in selected if cid in groups_map}
        if not selected:
            await msg.reply_text("❗ 当前选择群为空，已取消。", reply_markup=MAIN_KEYBOARD)
            context.user_data.clear()
            return

        post_id = gen_id()
        daily_time_raw = context.user_data[TEMP]["daily_time"]
        delete_minutes = int(context.user_data[TEMP].get("delete_minutes", 0))
        tm = parse_time_flexible(daily_time_raw)
        buttons = context.user_data.get(BUTTONS)
        content = content_from_message(msg)

        job_name = f"daily_{post_id}"

        posts = load_posts()
        posts.append({
            "id": post_id,
            "type": "daily",
            "groups": list(selected),
            "daily_time": daily_time_raw,
            "delete_minutes": delete_minutes,
            "content": content,
            "buttons": buttons,
            "enabled": True,
            "job_name": job_name,
        })
        save_posts(posts)

        context.job_queue.run_daily(
            daily_execute_job,
            time=tm,
            data={"post_id": post_id},
            name=job_name
        )

        await msg.reply_text(f"🔁 每日循环任务已创建（ID: {post_id}）", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear()
        return

async def daily_execute_job(context: ContextTypes.DEFAULT_TYPE):
    post_id = context.job.data.get("post_id")
    posts = load_posts()
    post = get_post(posts, post_id)
    if not post or not post.get("enabled", True):
        return

    groups = post.get("groups", [])
    content = post.get("content", {})
    buttons = post.get("buttons")
    delete_minutes = int(post.get("delete_minutes", 0))

    sent_msgs = []
    for cid in groups:
        try:
            m = await send_content(context, int(cid), content, buttons=buttons)
            sent_msgs.append({"chat_id": cid, "message_id": m.message_id})
        except Exception as e:
            logger.error(f"[每日发送失败] post={post_id} chat={cid} err={e}")

    if delete_minutes > 0 and sent_msgs and getattr(context, "job_queue", None) is not None:
        context.job_queue.run_once(
            delete_messages_job,
            when=delete_minutes * 60,
            data={"messages": sent_msgs}
        )

# =========================
# 我的帖子：查看/编辑/删除/启停
# =========================
async def my_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    posts = load_posts()
    if not posts:
        await update.message.reply_text("📭 暂无任何任务。", reply_markup=MAIN_KEYBOARD)
        return

    for p in posts:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔍 查看", callback_data=f"post_view:{p['id']}"),
                InlineKeyboardButton("✏️ 编辑内容", callback_data=f"post_edit:{p['id']}"),
            ],
            [
                InlineKeyboardButton("🗑 删除", callback_data=f"post_del:{p['id']}"),
                InlineKeyboardButton("⏹ 停用" if p.get("enabled", True) else "🔛 启用", callback_data=f"post_toggle:{p['id']}"),
            ]
        ])
        await update.message.reply_text(fmt_post(p), reply_markup=kb)

    await update.message.reply_text("以上为所有任务。", reply_markup=MAIN_KEYBOARD)

async def post_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("无权限")
        return
    post_id = q.data.split(":", 1)[1]
    posts = load_posts()
    post = get_post(posts, post_id)
    if not post:
        await q.answer("不存在")
        return
    content = post.get("content", {})
    summary = fmt_post(post)
    await q.message.reply_text(summary)
    if content.get("type") == "photo":
        await q.message.reply_photo(photo=content.get("photo_id"), caption="(图片内容预览)")
    else:
        await q.message.reply_text("📄 内容：\n" + (content.get("text") or ""))
    await q.answer("OK")

async def post_edit_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("无权限")
        return
    post_id = q.data.split(":", 1)[1]
    posts = load_posts()
    post = get_post(posts, post_id)
    if not post:
        await q.answer("不存在")
        return
    context.user_data.clear()
    context.user_data[MODE] = M_EDIT
    context.user_data[STEP] = S_AWAIT_CONTENT
    context.user_data[EDIT_POST_ID] = post_id
    await q.answer("请发送新内容")
    await q.message.reply_text("请发送新的内容（文字 或 图片+文字）。只改内容，不改时间/群/按钮。", reply_markup=ReplyKeyboardRemove())

async def post_edit_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get(MODE) != M_EDIT:
        return
    if context.user_data.get(STEP) != S_AWAIT_CONTENT:
        return

    msg = update.message
    post_id = context.user_data.get(EDIT_POST_ID)
    posts = load_posts()
    post = get_post(posts, post_id)
    if not post:
        await msg.reply_text("❗ 任务不存在或已删除。", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear()
        return

    post["content"] = content_from_message(msg)
    save_posts(posts)

    # schedule 未到时间：重建一次 job（确保更新内容生效）
    if post.get("type") == "schedule" and post.get("enabled", True) and ensure_job_queue(context):
        try:
            dt = datetime.fromisoformat(post.get("send_time"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=LOCAL_TZ)
            if dt > now_local():
                job_name = post.get("job_name", f"schedule_{post_id}")
                remove_jobs_by_name(context.job_queue, job_name)
                delay = max(1, int((dt - now_local()).total_seconds()))
                context.job_queue.run_once(
                    schedule_execute_job,
                    when=delay,
                    data={"post_id": post_id},
                    name=job_name
                )
        except Exception as e:
            logger.error(f"[编辑后重建 schedule job 失败] {e}")

    await msg.reply_text(f"✅ 已更新内容（ID: {post_id}）", reply_markup=MAIN_KEYBOARD)
    context.user_data.clear()

async def post_del_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("无权限")
        return
    post_id = q.data.split(":", 1)[1]
    posts = load_posts()
    post = get_post(posts, post_id)
    if not post:
        await q.answer("不存在")
        return
    job_name = post.get("job_name")
    if job_name and getattr(context, "job_queue", None) is not None:
        remove_jobs_by_name(context.job_queue, job_name)
    posts = [p for p in posts if p.get("id") != post_id]
    save_posts(posts)
    await q.answer("已删除")
    try:
        await q.message.delete()
    except Exception:
        pass

async def post_toggle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("无权限")
        return
    post_id = q.data.split(":", 1)[1]
    posts = load_posts()
    post = get_post(posts, post_id)
    if not post:
        await q.answer("不存在")
        return

    post["enabled"] = not post.get("enabled", True)

    job_name = post.get("job_name")
    if job_name and getattr(context, "job_queue", None) is not None:
        remove_jobs_by_name(context.job_queue, job_name)

    if post["enabled"] and ensure_job_queue(context):
        try:
            if post.get("type") == "daily":
                tm = parse_time_flexible(post.get("daily_time", ""))
                if tm:
                    context.job_queue.run_daily(
                        daily_execute_job,
                        time=tm,
                        data={"post_id": post_id},
                        name=job_name
                    )
            elif post.get("type") == "schedule":
                dt = datetime.fromisoformat(post.get("send_time"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=LOCAL_TZ)
                if dt > now_local():
                    delay = max(1, int((dt - now_local()).total_seconds()))
                    context.job_queue.run_once(
                        schedule_execute_job,
                        when=delay,
                        data={"post_id": post_id},
                        name=job_name
                    )
        except Exception as e:
            logger.error(f"[启用任务失败] {e}")

    save_posts(posts)
    await q.answer("已切换")
    try:
        await q.message.edit_text(fmt_post(post))
    except Exception:
        pass

# =========================
# Router（唯一消息入口）
# =========================
async def router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        return

    msg = update.message
    if not msg:
        return

    text = (msg.text or "").strip()
    mode = context.user_data.get(MODE)

    # 流程态
    if mode == M_IMMEDIATE:
        return await immediate_message(update, context)
    if mode == M_SCHEDULE:
        return await schedule_message(update, context)
    if mode == M_DAILY:
        return await daily_message(update, context)
    if mode == M_EDIT:
        return await post_edit_receive(update, context)

    # 空闲态：菜单
    if text == "📤 发送帖子":
        return await menu_send(update, context)
    if text == "📝 我的帖子":
        return await my_posts(update, context)
    if text == "🧩 群管理":
        return await managegroups(update, context)
    if text == "🧪 Debug":
        return await cmd_debug(update, context)

    if text == "🚀 立即发送":
        return await immediate_start(update, context)
    if text == "⏰ 定时发送":
        return await schedule_start(update, context)
    if text == "🔁 每日循环发送":
        return await daily_start(update, context)
    if text == "⬅️ 返回菜单":
        context.user_data.clear()
        return await msg.reply_text("已返回主菜单。", reply_markup=MAIN_KEYBOARD)

# =========================
# 启动恢复任务
# =========================
async def restore_jobs(app: Application):
    posts = load_posts()
    if not posts:
        logger.info("无任务可恢复")
        return

    if getattr(app, "job_queue", None) is None:
        logger.error("JobQueue 缺失：无法恢复任务。请确认 requirements.txt 使用 python-telegram-bot[job-queue,webhooks].")
        return

    restored = 0
    for p in posts:
        if not p.get("enabled", True):
            continue
        pid = p.get("id")
        ptype = p.get("type")
        job_name = p.get("job_name") or f"{ptype}_{pid}"
        p["job_name"] = job_name

        try:
            if ptype == "daily":
                tm = parse_time_flexible(p.get("daily_time", ""))
                if tm:
                    app.job_queue.run_daily(
                        daily_execute_job,
                        time=tm,
                        data={"post_id": pid},
                        name=job_name
                    )
                    restored += 1
            elif ptype == "schedule":
                dt = datetime.fromisoformat(p.get("send_time"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=LOCAL_TZ)
                if dt <= now_local():
                    continue
                delay = max(1, int((dt - now_local()).total_seconds()))
                app.job_queue.run_once(
                    schedule_execute_job,
                    when=delay,
                    data={"post_id": pid},
                    name=job_name
                )
                restored += 1
        except Exception as e:
            logger.error(f"[恢复失败] id={pid} type={ptype} err={e}")

    save_posts(posts)
    logger.info(f"恢复完成：{restored} 个任务")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled exception:", exc_info=context.error)

# =========================
# Webhook 启动
# =========================
def run_webhook(app: Application):
    if not WEBHOOK_BASE.startswith("https://"):
        raise RuntimeError("WEBHOOK_BASE 必须是 https:// 开头")

    url_path = f"telegram/webhook/{BOT_TOKEN}"
    webhook_url = f"{WEBHOOK_BASE}/{url_path}"

    logger.info(f"Webhook URL: {webhook_url}")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=url_path,
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN 为空，请在 Railway Variables 填 BOT_TOKEN")
    if not WEBHOOK_BASE:
        raise RuntimeError("WEBHOOK_BASE 为空，请在 Railway Variables 填 WEBHOOK_BASE")

    app = Application.builder().token(BOT_TOKEN).post_init(restore_jobs).build()

    # 命令
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("register", register_group))
    app.add_handler(CommandHandler("unregister", unregister_group))
    app.add_handler(CommandHandler("managegroups", managegroups))

    # callbacks
    app.add_handler(CallbackQueryHandler(managegroups_cb, pattern=r"^mg_"))
    app.add_handler(CallbackQueryHandler(immediate_cb, pattern=r"^im_"))
    app.add_handler(CallbackQueryHandler(schedule_cb, pattern=r"^sc_"))
    app.add_handler(CallbackQueryHandler(daily_cb, pattern=r"^dy_"))

    app.add_handler(CallbackQueryHandler(post_view_cb, pattern=r"^post_view:"))
    app.add_handler(CallbackQueryHandler(post_edit_cb, pattern=r"^post_edit:"))
    app.add_handler(CallbackQueryHandler(post_del_cb, pattern=r"^post_del:"))
    app.add_handler(CallbackQueryHandler(post_toggle_cb, pattern=r"^post_toggle:"))

    # router（唯一消息入口）
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, router))

    app.add_error_handler(on_error)

    logger.info("Starting BG678 Webhook Bot…")
    run_webhook(app)

if __name__ == "__main__":
    main()
