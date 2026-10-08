import io
import hashlib
import json
import os
import sys
import sqlite3
import random
import time
import math
import threading
import datetime
import asyncio
import traceback
import urllib.request
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont, ImageOps
import arabic_reshaper
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

RIYADH_TZ = ZoneInfo("Asia/Riyadh")

# إجبار stdout يطبع فوراً سطر بسطر بدل ما يتأخر بالذاكرة المؤقتة (buffering)
# بدون هذا السطر، رسائل print() ممكن ما تظهر بكونسول لوحة الاستضافة فوراً
# لحد ما البَفر يمتلئ أو البوت يتوقف — وهذا يخلي التشخيص اللحظي شبه مستحيل
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# تحميل متغيرات البيئة من ملف .env
load_dotenv()

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, max_messages=10000)

# Three presses per member/button in a rolling minute, across panel copies.
BUTTON_TOTAL_LIMIT = 10
BUTTON_PRESS_LIMIT = 3
BUTTON_PRESS_WINDOW = 60.0
_button_press_history = {}
_button_log_windows = {}
_button_last_cleanup = 0.0


def register_button_press(guild_id, user_id, button_id, now):
    global _button_last_cleanup
    if now - _button_last_cleanup >= BUTTON_PRESS_WINDOW:
        for key in list(_button_press_history):
            recent = [stamp for stamp in _button_press_history[key] if now - stamp < BUTTON_PRESS_WINDOW]
            if recent:
                _button_press_history[key] = recent
            else:
                _button_press_history.pop(key, None)
        for key, stamp in list(_button_log_windows.items()):
            if now - stamp >= BUTTON_PRESS_WINDOW:
                _button_log_windows.pop(key, None)
        _button_last_cleanup = now
    total_key = (guild_id, user_id, None)
    total = [stamp for stamp in _button_press_history.get(total_key, ()) if now - stamp < BUTTON_PRESS_WINDOW]
    _button_press_history[total_key] = total
    if len(total) >= BUTTON_TOTAL_LIMIT:
        wait = max(1, math.ceil(BUTTON_PRESS_WINDOW - (now - total[0])))
        log = now - _button_log_windows.get(total_key, -float("inf")) >= BUTTON_PRESS_WINDOW
        if log:
            _button_log_windows[total_key] = now
        return False, wait, log
    key = (guild_id, user_id, button_id)
    recent = [stamp for stamp in _button_press_history.get(key, ()) if now - stamp < BUTTON_PRESS_WINDOW]
    _button_press_history[key] = recent
    if len(recent) < BUTTON_PRESS_LIMIT:
        recent.append(now)
        total.append(now)
        return True, 0, False
    wait = max(1, math.ceil(BUTTON_PRESS_WINDOW - (now - recent[0])))
    log = now - _button_log_windows.get(key, -float("inf")) >= BUTTON_PRESS_WINDOW
    if log:
        _button_log_windows[key] = now
    return False, wait, log


class ButtonLimitedView(discord.ui.View):
    # Run before callbacks and even before a subclass's permission check.
    async def _scheduled_task(self, item, interaction):
        if not isinstance(item, discord.ui.Button):
            return await super()._scheduled_task(item, interaction)
        button_id = str(getattr(item, "custom_id", None) or getattr(item, "label", "button"))
        allowed, wait, log = register_button_press(interaction.guild_id, interaction.user.id, button_id, time.monotonic())
        if allowed:
            return await super()._scheduled_task(item, interaction)
        label = str(getattr(item, "label", None) or button_id)[:150]
        print(f"[Button Limit] user={interaction.user.id} guild={interaction.guild_id} button={button_id!r}: blocked (3/min)")
        try:
            await interaction.response.send_message(
                f"وصلت حد الضغطات: 3 لكل زر و10 لجميع الأزرار خلال دقيقة. تقدر تضغطه بعد {wait} ثانية.", ephemeral=True)
        except discord.HTTPException as exc:
            print(f"[Button Limit] Could not acknowledge: HTTP {exc.status}")
        if log:
            channel = interaction.client.get_channel(LOG_CHANNEL_ID)
            if channel:
                try:
                    embed = discord.Embed(title="تنبيه: تكرار ضغط زر", color=discord.Color.orange())
                    embed.add_field(name="العضو", value=f"{interaction.user.mention}\n{interaction.user}\n`{interaction.user.id}`", inline=False)
                    embed.add_field(name="الزر", value=label, inline=False)
                    embed.add_field(name="التفاصيل", value="تجاوز حد الضغطات: 3 لكل زر أو 10 لجميع الأزرار خلال دقيقة؛ مُنعت الضغطات الإضافية.", inline=False)
                    if interaction.channel_id:
                        embed.add_field(name="الروم", value=f"<#{interaction.channel_id}>")
                    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException as exc:
                    print(f"[Button Limit] Could not send audit log: HTTP {exc.status}")


# ----------------- الإعدادات والآيديات -----------------
WELCOME_CHANNEL_ID = 1530748598411853964
LOG_CHANNEL_ID = 1532594322153799690               # روم ⚙️┊سـجـلات-الـسـيـرفـر
GIVEAWAY_CHANNEL_ID = 1544343705635524749         # آيدي روم الألعاب المجانية
LEVEL_CHANNEL_ID = 1544500656851779626             # آيدي روم إشعارات التلفيل والترقيات
WEEKLY_LEADERBOARD_CHANNEL_ID = 1388414282739224727 # آيدي روم نشر ليدربورد نهاية الأسبوع
MONEY_LEADERBOARD_CHANNEL_ID = WEEKLY_LEADERBOARD_CHANNEL_ID # آيدي روم نشر ليدربورد أغنى الأعضاء كل 3 أيام (غيّرها لروم ثاني لو تبي)
BOOST_CHANNEL_ID = 1544476010693656577             # روم 🌟┊داعـمـيـن-الـسـيـرفـر
# مكافأة مالية تُعطى تلقائياً لكل عضو يعمل Boost للسيرفر (اتركها 0 لتعطيل المكافأة بدون التأثير على رسالة الشكر)
BOOST_REWARD_AMOUNT = 100
SUGGESTIONS_CHANNEL_ID = 1388414282739224727                          # روم نشر اقتراحات الأعضاء (حط آيدي الروم وإلا أمر /suggest ما يشتغل)

# رتبة النائب
QUEST_REMINDER_CHANNEL_ID = 1388414282739224727
QUEST_START_CHANNEL_ID = 1545393184291688458
QUEST_REMINDER_TIME = datetime.time(hour=20, minute=0, tzinfo=RIYADH_TZ)
CONTROL_PANEL_GUIDE_TIME = datetime.time(hour=14, minute=0, tzinfo=RIYADH_TZ)

DEPUTY_ROLE_ID = 1364644574093377647

# رتبة إضافية مسموح لها تحديداً باستخدام أمر /warn (إعطاء الإنذارات) فقط، بجانب الأونر والنائب وستاف EXTRA_STAFF_ROLE_IDS
WARN_EXTRA_ROLE_ID = 1550552628939718706

# مبلغ الخصم التلقائي من رصيد العضو مع كل إنذار (اتركه 0 لتعطيل الخصم)
WARNING_MONEY_PENALTY = 100

# خانات فاضية إضافية لرتب ثانية تبيها تاخذ نفس صلاحيات النائب بكل النظام
# (الأوامر الإدارية، فتح/إغلاق التذاكر، لوحة الفويس الثابتة، رسالة VIP بالفويس... إلخ)
# اكتب آيدي الرتبة بمكان أي 0 وقت ما تبي، بدون ما تحتاج تعدل أي كود
EXTRA_STAFF_ROLE_IDS = [0, 0, 0, 0, 0]

def is_staff_member(member: discord.Member) -> bool:
    """يرجع True لو العضو أونر أو نائب أو أي رتبة من EXTRA_STAFF_ROLE_IDS."""
    if member.guild and member.id == member.guild.owner_id:
        return True
    role_ids = {r.id for r in member.roles}
    if DEPUTY_ROLE_ID != 0 and DEPUTY_ROLE_ID in role_ids:
        return True
    return any(rid != 0 and rid in role_ids for rid in EXTRA_STAFF_ROLE_IDS)

# القنوات الصوتية الثابتة (غير المؤقتة) اللي تُرسل فيها لوحة تحكم خاصة
# بالقائد (صاحب السيرفر) والنائب فقط عبر أمر /setup_leader_voice_panel
LEADER_DEPUTY_VOICE_PANEL_CHANNEL_IDS = [
    1538085780218581083,
    1252454588653764645,
    0, 0, 0, 0, 0,  # خانات فاضية: ضيف آيدي أي روم صوتي ثابت تبي تعطيه نفس اللوحة
]

# رومات إنشاء الفويس المؤقت (روم الإنشاء + روم المشاهدة)
TEMP_VOICE_CREATOR_IDS = [
   0, 0, 0, 0, 0,  # خانات فاضية: ضيف آيدي أي روم إنشاء فويس مؤقت إضافي
]

# ----------------- إعدادات مكافأة إرسال الفيديو -----------------
# الروم اللي لو أرسل فيه العضو فيديو يُعطى مكافأة مالية تلقائية
VIDEO_REWARD_CHANNEL_ID = 1364641740912595014
# قيمة المكافأة عن كل فيديو
VIDEO_REWARD_AMOUNT = 5
# أقصى مبلغ يقدر العضو يكسبه من هذي الميزة باليوم (يعني أقصى فيديوهين مدفوعين باليوم)
VIDEO_REWARD_DAILY_CAP = 10
# روم إشعار العضو بإضافة الرصيد (بدون كتابة الرصيد الحالي حفاظاً على الخصوصية)
VIDEO_REWARD_ANNOUNCE_CHANNEL_ID = 1388414282739224727
# قيمة المكافأة عن كل صورة (نفس فكرة مكافأة الفيديو، لكن للصور) بنفس روم اللقطات
PHOTO_REWARD_AMOUNT = 5

# ----------------- إعدادات مسابقة "أفضل لقطة أسبوعية" (تصويت بروم اللقطات) -----------------
# أي رسالة فيها صورة أو فيديو بنفس روم اللقطات (VIDEO_REWARD_CHANNEL_ID) يرسل تحتها البوت
# رسالة فيها زر تصويت + عداد إجمالي التصويتات. آخر كل أسبوع (نفس توقيت WEEKLY_POST_TIME)
# يُحسب صاحب أكثر لقطة تصويتات ويُكافأ ويُعلن عنه، وترجع المسابقة تبدأ من الصفر للأسبوع الجديد.
BEST_CLIP_CHANNEL_ID = VIDEO_REWARD_CHANNEL_ID
# مكافأة صاحب أفضل لقطة بنهاية الأسبوع
BEST_CLIP_WEEKLY_REWARD = 50
BEST_CLIP_MIN_VOTES = 2
_clip_panel_refresh_job = None
# روم إعلان الفائز بأفضل لقطة الأسبوع (نفس روم الإعلانات العام)
BEST_CLIP_ANNOUNCE_CHANNEL_ID = 1388414282739224727
# التصويت محصور بالأعضاء المحددين، دون اعتماد على الرتب.
BEST_CLIP_VOTER_USER_IDS = [
    1367448003757084672, 1337500699004506122, 1384519174822563965,
    767874265932234763, 630743401238888478, 1441934919902171176,
    204682468060692480, 928859305657118780, 767838210302410772,
    1212149609607209010, 929066176401772586,
]

# لو تبي تحصر مكافأة الروابط (مو الملفات المرفوعة مباشرة) بمواقع معينة بس، ضيف جزء من الدومين هنا
# (مثال: "medal.tv", "streamable.com", "outplayed.tv", "clips.twitch.tv"). خله قائمة فاضية عشان
# يقبل أي رابط يقدر ديسكورد يسوي له معاينة فيديو/صورة تلقائياً (الأسهل والأشمل).
CLIP_LINK_ALLOWED_DOMAINS = []

def can_vote_on_clips(member: discord.Member) -> bool:
    return bool(member.guild and member.id in BEST_CLIP_VOTER_USER_IDS)

# ----------------- إعدادات نظام التسجيل اليومي (الحضور اليومي) -----------------
# يُحتسب "تسجيل" لأي عضو يرسل رسالة أو يدخل روم صوتي، بحد أقصى مرة وحدة باليوم (بتوقيت الرياض)
# الريالات ما تُعطى أول مرة يسجل فيها العضو (ستريك = 1)، وتبدأ من ثاني يوم متتالي (ستريك >= 2)
DAILY_CHECKIN_REWARD = 5
# الـ XP تُعطى مع كل تسجيل يومي ناجح (حتى أول مرة)
DAILY_CHECKIN_XP = 25
# روم نشر إشعار التسجيل اليومي (الستريك + المكافأة المكتسبة)
DAILY_CHECKIN_ANNOUNCE_CHANNEL_ID = 1388414282739224727

MEMBER_ROLE_ID = 1532604564325269684
BOT_ROLE_ID = 1530740824436899860

# الرومات الصوتية المستثناة من شرط الميوت لاكتساب XP (يكتسب XP حتى لو ميوت، بشرط مو ديف)
EXEMPT_MUTE_VOICE_CHANNELS = [
    1538483515438661644,
    1538085780218581083,
    1547362226405511219,
    0, 0, 0,  # خانات فاضية: ضيف آيدي أي روم صوتي تبي تستثنيه من شرط الميوت
]

# ----------------- إعدادات نظام العملة (البنك + الرواتب الشهرية) -----------------
CURRENCY_NAME = "ريال"
CURRENCY_EMOJI = "<:riyal:1548799345317384232>"

# مكافأة مالية تُعطى تلقائياً للعضو مع كل لفل جديد يوصله (كتابة، صوت، أو عبر /add_xp)
LEVEL_UP_REWARD = 50

# ----------------- إعدادات الروليت اليومي -----------------
# كل عنصر: (الاسم الظاهر، الإيموجي، النوع، القيمة، الوزن النسبي، لون القطعة بعجلة الدوران البصرية RGB)
# النوع: "win" = يربح مبلغ، "retry" = يعيد الدورة مجاناً بدون ما تحتسب خسارة أو ربح، "none" = هارد لك ما يربح شي
# الوزن كل ما زاد رفع احتمال الوقوع عليه (هو المتحكم الفعلي بالفرص). المجموع الحالي 100 لسهولة القراءة كنسبة مئوية
# ملاحظة: حجم قطعة كل خانة برسم العجلة متساوٍ بصرياً (العجلة مقسّمة أقسام متساوية) — الوزن هو اللي يتحكم فعلياً بالاحتمال، مو حجم القطعة
# شكل العجلة بدرجات الأزرق وإطار ذهبي؛ الإعادة ذهبية وخانة هارد لك حمراء.
ROULETTE_SEGMENTS = [
    ("5", "⚫", "win", 5, 15, (15, 15, 15)),
    ("10", "⚪", "win", 10, 12, (235, 235, 235)),
    ("20", "⚫", "win", 20, 8, (15, 15, 15)),
    ("30", "⚪", "win", 30, 5, (235, 235, 235)),
    ("40", "⚫", "win", 40, 3, (15, 15, 15)),
    ("50", "⚪", "win", 50, 2, (235, 235, 235)),
    ("إعادة", "🔁", "retry", 0, 8, (150, 150, 150)),
    ("هارد لك", "💀", "none", 0, 47, (200, 30, 30)),
]
# حد أقصى لعدد مرات "إعادة" المتتالية بنفس الدورة، حماية نظرية من حلقة تدور بلا نهاية
ROULETTE_MAX_RETRIES = 5

# لو العضو ربح هالمبلغ أو أكثر بالروليت، يُرسل إشعار عام بهالروم يهنّئه
ROULETTE_BIG_WIN_THRESHOLD = 30
ROULETTE_BIG_WIN_CHANNEL_ID = 1388414282739224727

# تدوير الروليت محصور بالأعضاء المحددين فقط.
ROULETTE_ALLOWED_USER_IDS = [
    1367448003757084672, 1337500699004506122, 1384519174822563965,
    767874265932234763, 630743401238888478, 1441934919902171176,
    204682468060692480, 767838210302410772, 928859305657118780,
    1212149609607209010, 929066176401772586,
]



# رواتب شهرية تلقائية حسب الرتبة: كل عضو عنده رتبة من هذي ياخذ المبلغ المقابل لها
# آخر يوم بكل شهر (بتوقيت الرياض). لو عضو عنده أكثر من رتبة من القائمة، تُجمع له كل الرواتب مع بعض.
SALARY_ROLES = {
    1388428431041105940: 250,
    1364643927537488005: 100,
    1532611254856847400: 100,
}

# روم إعلان صرف الرواتب: يُرسل فيه إشعار واحد فقط للجميع بعد كل عملية صرف (بالإضافة للتفاصيل اللي تروح لروم السجلات)
SALARY_ANNOUNCE_CHANNEL_ID = 1388414282739224727

# رواتب شهرية مباشرة لأعضاء محددين، تُضاف إلى أي راتب رتبة مؤهلة.
SALARY_MEMBERS = {
    1337500699004506122: 100,
    928859305657118780: 100,
    1384519174822563965: 100,
    767838210302410772: 100,
    1441934919902171176: 100,
    1367448003757084672: 100,
    767874265932234763: 100,
    929066176401772586: 100,
}

# ----------------- إعدادات تذكير الصلاة -----------------
# الروم النصي الرئيسي اللي يوصله تذكير كل صلاة
PRAYER_REMINDER_CHANNEL_ID = 1549351115236319284
# المدينة/الدولة المستخدمة لجلب مواقيت الصلاة من خدمة Aladhan (افتراضياً الرياض - غيّرها لو السيرفر بمدينة/دولة ثانية)
PRAYER_CITY = "Riyadh"
PRAYER_COUNTRY = "Saudi Arabia"
PRAYER_CALC_METHOD = 4  # 4 = أم القرى، المعتمدة رسمياً بالسعودية
# أسماء الصلوات الخمس بالعربي — المفاتيح هنا لازم تطابق أسماء استجابة الـ API بالضبط (Fajr, Dhuhr, Asr, Maghrib, Isha)
PRAYER_NAMES_AR = {
    "Fajr": "الفجر",
    "Dhuhr": "الظهر",
    "Asr": "العصر",
    "Maghrib": "المغرب",
    "Isha": "العشاء",
}

# ----------------- إعدادات تشغيل صوت الأذان بالرومات الصوتية -----------------
# هذي الميزة تخلي البوت يدخل الرومات الصوتية اللي فيها أعضاء وقت الصلاة ويشغّل الأذان،
# وهي أقوى من المنشن لأن العضو يسمعه وهو يلعب بدون ما يحتاج يفعّل أي شيء بإعداداته.
# تحتاج: تثبيت PyNaCl (pip install PyNaCl) + وجود ffmpeg على السيرفر/الاستضافة.
ADHAN_VOICE_ENABLED = True
# مسار ملف صوتي محلي (يُرفع جنب main.py) أو رابط mp3 مباشر
ADHAN_AUDIO_SOURCE = "adhan.mp3"
# ملف خاص بأذان الفجر (فيه "الصلاة خير من النوم") — خلّه فاضي عشان يستخدم الملف العادي
ADHAN_FAJR_AUDIO_SOURCE = ""
# أقصى مدة تشغيل بالثواني لكل روم (حماية من ملف طويل يعلّق البوت بالروم)
ADHAN_MAX_SECONDS = 90
# أقصى عدد رومات صوتية يدخلها البوت بنفس السيرفر (يبدأ بالأكثر أعضاءً)
# ملاحظة: البوت ما يقدر يكون بأكثر من روم صوتي واحد بنفس السيرفر، فيمرّ عليها واحد واحد
ADHAN_MAX_CHANNELS_PER_GUILD = 3
# أقل عدد أعضاء حقيقيين بالروم عشان يدخله البوت
ADHAN_MIN_HUMANS = 1
# مستوى الصوت (1.0 = الأصلي)
ADHAN_VOLUME = 0.7

# ----------------- إعدادات وقوائم الأذكار / الصلاة على النبي / الأحاديث -----------------
# (تُرسل كلها بنفس روم تذكير الصلاة أعلاه: PRAYER_REMINDER_CHANNEL_ID)
MORNING_ADHKAR_TIME = datetime.time(hour=6, minute=0, second=0, tzinfo=ZoneInfo("Asia/Riyadh"))   # بعد الفجر تقريباً
EVENING_ADHKAR_TIME = datetime.time(hour=16, minute=30, second=0, tzinfo=ZoneInfo("Asia/Riyadh"))  # بعد العصر تقريباً
HADITH_TIME = datetime.time(hour=12, minute=0, second=0, tzinfo=ZoneInfo("Asia/Riyadh"))
SALAWAT_INTERVAL_HOURS = 4  # تذكير بالصلاة على النبي ﷺ كل كم ساعة

MORNING_ADHKAR = [
    "أصبحنا وأصبح الملك لله، والحمد لله، لا إله إلا الله وحده لا شريك له، له الملك وله الحمد وهو على كل شيء قدير.",
    "اللهم بك أصبحنا، وبك أمسينا، وبك نحيا، وبك نموت، وإليك النشور.",
    "اللهم أنت ربي لا إله إلا أنت، خلقتني وأنا عبدك، وأنا على عهدك ووعدك ما استطعت، أعوذ بك من شر ما صنعت، أبوء لك بنعمتك علي، وأبوء بذنبي فاغفر لي فإنه لا يغفر الذنوب إلا أنت (سيد الاستغفار).",
    "اللهم عافني في بدني، اللهم عافني في سمعي، اللهم عافني في بصري، لا إله إلا أنت.",
    "اللهم إني أسألك العفو والعافية في الدنيا والآخرة.",
    "بسم الله الذي لا يضر مع اسمه شيء في الأرض ولا في السماء وهو السميع العليم (تُقال 3 مرات).",
    "رضيت بالله رباً، وبالإسلام ديناً، وبمحمد صلى الله عليه وسلم نبياً.",
    "حسبي الله لا إله إلا هو عليه توكلت وهو رب العرش العظيم (تُقال 7 مرات).",
]

EVENING_ADHKAR = [
    "أمسينا وأمسى الملك لله، والحمد لله، لا إله إلا الله وحده لا شريك له، له الملك وله الحمد وهو على كل شيء قدير.",
    "اللهم بك أمسينا، وبك أصبحنا، وبك نحيا، وبك نموت، وإليك المصير.",
    "اللهم أنت ربي لا إله إلا أنت، خلقتني وأنا عبدك، وأنا على عهدك ووعدك ما استطعت، أعوذ بك من شر ما صنعت، أبوء لك بنعمتك علي، وأبوء بذنبي فاغفر لي فإنه لا يغفر الذنوب إلا أنت (سيد الاستغفار).",
    "اللهم عافني في بدني، اللهم عافني في سمعي، اللهم عافني في بصري، لا إله إلا أنت.",
    "اللهم إني أسألك العفو والعافية في الدنيا والآخرة.",
    "بسم الله الذي لا يضر مع اسمه شيء في الأرض ولا في السماء وهو السميع العليم (تُقال 3 مرات).",
    "رضيت بالله رباً، وبالإسلام ديناً، وبمحمد صلى الله عليه وسلم نبياً.",
    "أعوذ بكلمات الله التامات من شر ما خلق (تُقال 3 مرات).",
]

SALAWAT_PHRASES = [
    "اللهم صلِّ وسلم على نبينا محمد، وعلى آله وصحبه أجمعين 🤍",
    "اللهم صلِّ على محمد وعلى آل محمد، كما صليت على إبراهيم وعلى آل إبراهيم، إنك حميد مجيد.",
    "لا تنسوا الصلاة على رسول الله ﷺ، فمن صلى عليه صلاة صلى الله عليه بها عشراً.",
    "اللهم صلِّ وسلم وبارك على سيدنا محمد عدد ما ذكره الذاكرون وغفل عن ذكره الغافلون.",
    "من أكثر الصلاة على رسول الله ﷺ كان أولى الناس به يوم القيامة، فأكثروا منها 🤍",
    "اللهم صلِّ على محمد النبي الأمي وعلى آله وسلم تسليماً.",
]

# كل عنصر عبارة عن (نص الحديث، مصدره)
HADITHS = [
    ("إنما الأعمال بالنيات، وإنما لكل امرئ ما نوى.", "متفق عليه"),
    ("المسلم من سلم المسلمون من لسانه ويده.", "متفق عليه"),
    ("لا يؤمن أحدكم حتى يحب لأخيه ما يحب لنفسه.", "متفق عليه"),
    ("من كان يؤمن بالله واليوم الآخر فليقل خيراً أو ليصمت.", "متفق عليه"),
    ("الدين النصيحة، قلنا: لمن؟ قال: لله ولكتابه ولرسوله ولأئمة المسلمين وعامتهم.", "رواه مسلم"),
    ("من حسن إسلام المرء تركه ما لا يعنيه.", "رواه الترمذي"),
    ("الكلمة الطيبة صدقة.", "متفق عليه"),
    ("تبسمك في وجه أخيك صدقة.", "رواه الترمذي"),
    ("من لا يرحم الناس لا يرحمه الله.", "متفق عليه"),
    ("خيركم من تعلم القرآن وعلمه.", "رواه البخاري"),
    ("اتق الله حيثما كنت، وأتبع السيئة الحسنة تمحها، وخالق الناس بخلق حسن.", "رواه الترمذي"),
    ("لا يدخل الجنة من كان في قلبه مثقال ذرة من كبر.", "رواه مسلم"),
    ("من صمت نجا.", "رواه الترمذي"),
    ("الطهور شطر الإيمان.", "رواه مسلم"),
    ("احرص على ما ينفعك واستعن بالله ولا تعجز.", "رواه مسلم"),
]

# ----------------- التوكن يُقرأ من متغيرات البيئة وليس مكتوباً بالكود -----------------
# ضع التوكن في ملف .env بجانب هذا الملف بهذا الشكل:
# BOT_TOKEN=توكنك_هنا
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "❌ لم يتم العثور على BOT_TOKEN. تأكد من إنشاء ملف .env يحتوي على السطر:\nBOT_TOKEN=توكنك_هنا"
    )

MAX_LEVEL = 50
VOICE_DAILY_CAP = 1000
TEXT_XP_COOLDOWN_SECONDS = 30

# ----------------- إعدادات نظام الإنذارات التلقائي -----------------
# لما عدد إنذارات العضو يوصل بالضبط لهذا الرقم، يُعطى تايم أوت تلقائي (بدون تدخل يدوي)
WARNINGS_AUTO_TIMEOUT_THRESHOLD = 3
# مدة التايم أوت التلقائي بالساعات
WARNINGS_AUTO_TIMEOUT_HOURS = 24
# روم نشر إشعار كل إنذار (بالإضافة لروم السجلات)
WARNING_ANNOUNCE_CHANNEL_ID = 1388414282739224727
# الإنذار ينتهي/يُحذف تلقائياً بعد هالمدة (بالأيام) — يعني ما يضل يُحسب على العضو للأبد
WARNING_EXPIRY_DAYS = 30
# لما عدد إنذارات العضو (غير المنتهية) يوصل لهذا الرقم، يُسحب منه الرول المحدد تلقائياً
WARNINGS_ROLE_STRIP_THRESHOLD = 5
# الرول اللي ينسحب من العضو عند الوصول لعتبة السحب أعلاه (افتراضياً رول العضوية MEMBER_ROLE_ID)
WARNING_STRIP_ROLE_ID = MEMBER_ROLE_ID

# ----------------- إعدادات تسجيل مشاركة الشاشة و AFK بروم السجلات -----------------
# لو العضو مسكر مايكه (self mute) بشكل متواصل لهالمدة (بالدقايق)، يُسجل بروم السجلات كـ AFK محتمل
# (تسجيل مرة وحدة بس لكل فترة سكوت، يرجع يسجل من جديد لو فتح مايكه وسكره مرة ثانية)
AFK_MUTE_THRESHOLD_MINUTES = 5 * 60  # 5 ساعات
# فحص AFK يشتغل بس بهالرومات الصوتية المحددة (قائمة سماح) — أي روم مو موجود هنا ما يُفحص إطلاقاً
AFK_TRACKED_CHANNEL_IDS = [
    1547352785610080288,
    1545796827927023736,
    1545796924869976175,
]
# الرومات الصوتية المستثناة من فحص AFK حتى لو كانت بقائمة AFK_TRACKED_CHANNEL_IDS
# (ما يُسجل فيها أبداً حتى لو مسكر مايكه لمدة طويلة)
AFK_EXCLUDED_CHANNEL_IDS = [1538085780218581083]

# آيديات رومات الإحصائيات الصوتية الـ 4 المتبقية
STAT_ALL_MEMBERS_ID = 1533202355825283204  # All Members & Bots
STAT_MEMBERS_ID = 1533204199486259311      # Members
STAT_BOTS_ID = 1533203279255699554         # Bots
STAT_ONLINE_ID = 1544289822515011644       # Online

# آيدي روم إحصائية "In Voice" — ضع الآيدي الصحيح هنا إذا تبي تفعيل هذه الميزة
# إذا تركته 0 فالبوت ببساطة يتجاهل تحديث هذا الروم بدون أي خطأ
STAT_IN_VOICE_ID = 1550139696670187570

# قائمة الرومات الصوتية المسموح بالتلفيل فيها سولو
SOLO_VOICE_CHANNELS = [
    1538085780218581083,
   1538483515438661644,
    0, 0, 0, 0, 0,  # خانات فاضية: ضيف آيدي أي روم صوتي تبي تسمح فيه بالتلفيل سولو
]

# ----------------- إعدادات نظام تذاكر الدعم الفني -----------------
# آيدي الكاتيجوري اللي بتنشأ بداخله رومات التذاكر (اتركه 0 لو تبي تُنشأ بدون كاتيجوري محدد)
TICKET_CATEGORY_ID = 1545764448147349585

# رتب الدعم الفني المسموح لها ترى وترد على كل التذاكر (بالإضافة للنائب وصاحب السيرفر)
# حط لين 5 آيديات رولات هنا، مثال: [111111111111111111, 222222222222222222]
# اتركها قائمة فاضية [] إذا ما تبي رتب إضافية
TICKET_SUPPORT_ROLE_IDS = [1388428654320816128, 0, 0, 0, 0]

# أنواع التذاكر اللي تظهر بالقائمة المنسدلة عند فتح تذكرة جديدة
# كل عنصر: (المفتاح الداخلي، الاسم الظاهر، الإيموجي، وصف قصير يظهر تحت الاسم بالقائمة)
# تقدر تغيّر الأسماء أو تضيف/تحذف أنواع (الحد الأقصى المسموح من ديسكورد للقائمة 25 خيار)
TICKET_TYPES = [
    ("general",   "استفسار عام", "❓", "أسئلة عامة عن السيرفر"),
    ("complaint", "شكوى",        "⚠️", "شكوى بخصوص عضو أو مشكلة"),
    ("report",    "بلاغ",        "🚨", "بلاغ عن مخالفة أو مشكلة تقنية"),
    ("support",   "دعم فني",     "🛠️", "مشاكل تقنية بالسيرفر أو البوت"),
]

# ----------------- إعدادات نظام أعياد الميلاد -----------------
# آيدي الروم اللي بينشر فيه البوت تهنئة عيد الميلاد (لازم تحطه وإلا الميزة ما تشتغل)
BIRTHDAY_CHANNEL_ID = 1388414282739224727
# رتبة مؤقتة تُعطى لصاحب عيد الميلاد ليوم واحد فقط (اختياري، اتركها 0 لتعطيلها)
BIRTHDAY_ROLE_ID = 1545407469734334524
# مكافأة مالية تُعطى تلقائياً لمن يحصل على رتبة المناسبات الخاصة (عيد ميلاد / ذكرى انضمام / ذكرى حساب)
BIRTHDAY_ROLE_REWARD = 100

# ----------------- إعدادات النظام الملكي (مراسيم رسمية) -----------------
# آيدي روم الإعلانات اللي تُنشر فيه المراسيم الرسمية (لازم تحطه وإلا أمر /decree ما يشتغل)
DECREE_CHANNEL_ID = 0
# آيدي الروم اللي تُنشر فيه إعلانات الاستدعاء الملكي والخطاب الملكي، إضافة على الروم الصوتي نفسه
ROYAL_ANNOUNCE_CHANNEL_ID = 1388414282739224727

# ----------------- رابط الصورة الصغيرة لبانل روكت ليق -----------------
RL_PANEL_THUMBNAIL_URL = "https://cdn.discordapp.com/attachments/1530751122766037127/1544488486734930072/s15rank22.webp?ex=6a98b07e&is=6a975efe&hm=78fb6aa0b3bda414d819b555106730f29f5c510ecd4ec4f73b802027fbd4ee0b&"

# ----------------- إعدادات رتب روكت ليق مع السوبر والقراند (موزعة لكي لا يتجاوز الصف 5 أزرار) -----------------
RL_RANKS_CONFIG = [
    ('Grand Champion III', '<:gc3:1539276356779507752>', 1532619948462178374, 0),
    ('Grand Champion II', '<:gc2:1539276501848162304>', 1532619900005257296, 0),
    ('Grand Champion I', '<:gc1:1539276444771946586>', 1388416938660855818, 0),
    ('Champion III', '<:c3:1532626169105481728>', 1532619660661493941, 0),
    ('Champion II', '<:c2:1532626411716874341>', 1532619718454808606, 0),
    ('Champion I', '<:c1:1532626535285129276>', 1388417599921983498, 1),
    ('Diamond III', '<:d3:1532626858120581181>', 1532625388663214162, 1),
    ('Diamond II', '<:d2:1532626911875043329>', 1532625490031018075, 1),
    ('Diamond I', '<:d1:1532627022633766972>', 1532625522364907661, 1),
    ('Platinum III', '<:p3:1532627235562065930>', 1532625870420971550, 1),
    ('Platinum II', '<:p2:1532627292721905705>', 1532625925751967764, 2),
    ('Platinum I', '<:p1:1532627334102777936>', 1532625955401498705, 2),
    ('Gold III', '<:g3:1532634337210536086>', 1532634004996620308, 2),
    ('Gold II', '<:g2:1532634376888651776>', 1532634096377794671, 2),
    ('Gold I', '<:g1:1532634410992533555>', 1532634096377794671, 2),
    ('Silver III', '<:s3:1532635053807632496>', 1532634715780284526, 3),
    ('Silver II', '<:s2:1532635085977682051>', 1532634773942567063, 3),
    ('Silver I', '<:s1:1532635155498274936>', 1532634807840931840, 3),
    ('Bronze III', '<:b3:1532635783121338399>', 1532635542515351553, 3),
    ('Bronze II', '<:b2:1532635783121338399>', 1532635652385017977, 3),
    ('Bronze I', '<:b1:1532635845922652180>', 1532635685889114122, 4),
    ('Unranked', '<:u:1539080651410776225>', 1539080813780533248, 4),
]

temp_channels = {}
ticket_channels = {}  # channel_id -> user_id صاحب التذكرة
royal_address_active = {}  # voice_channel_id -> [قائمة آيديات الأعضاء اللي كتمهم البوت أثناء الخطاب]
channel_update_cooldowns = {}
vip_voice_cooldowns = {}
vip_voice_messages = {}
voice_timers = {}
mute_start_times = {}  # member_id -> الوقت (time.time()) اللي بدأ فيه ميوت المايك (self_mute) بشكل متواصل
afk_logged_members = set()  # member_id: تم تسجيل "AFK" له بروم السجلات، ما يتكرر التسجيل لحد ما يفتح مايكه
suggestion_votes = {}  # message_id -> {"up": set(user_id), "down": set(user_id)}
invite_cache = {}  # guild_id -> {invite_code: {"uses": int, "inviter_id": int, "max_uses": int}}

LEVEL_ROLES = {
    1: 1532247076694659142, 2: 1533071385654460516, 3: 1533071452436299836, 4: 1533071484958806057,
    5: 1532247799624765561, 6: 1533071526805635082, 7: 1533071564113707048, 8: 1533071631465844748,
    9: 1533071664689057824, 10: 1532247833808343191, 11: 1533071816283783279, 12: 1533071905152827443,
    13: 1533071937188790392, 14: 1533071972983115846, 15: 1532247871976374332, 16: 1533072100137504930,
    17: 1533072139228282970, 18: 1533072170740093008, 19: 1533072200993869865, 20: 1532247915605655553,
    21: 1533072309013708990, 22: 1533072357470634085, 23: 1533072421702340698, 24: 1533072449699053638,
    25: 1532247962296778914, 26: 1533072538102399048, 27: 1533072711386009741, 28: 1533072677038854175,
    29: 1533072604620128377, 30: 1532248035328004229, 31: 1533072819565625487, 32: 1533072856483762197,
    33: 1533072887290921133, 34: 1533072916223099010, 35: 1532248166668304607, 36: 1533073073635328080,
    37: 1533073042622644274, 38: 1533073010104209481, 39: 1533072949274480803, 40: 1532248191586533518,
    41: 1533073155118071959, 42: 1533073199393407096, 43: 1533073236508672082, 44: 1533073273187733705,
    45: 1532248222054088794, 46: 1533073358919307454, 47: 1533073394910888036, 48: 1533073424220553366,
    49: 1533073316481601667, 50: 1532248252995338300,
}

ICON_GIF_URL = "https://cdn.discordapp.com/attachments/1530751122766037127/1539951579266814023/B4E_neon_hollow_512x512.gif?ex=6a97586b&is=6a9606eb&hm=5879759df1ee6ddefe15e9f7bc8e5ed736b543a51b40d04df3da163cc5f9b88b&"
# الصورة الكبيرة (البانر) اللي تظهر أسفل الإيمبيدات
BANNER_IMAGE_URL = "https://i.imgur.com/pgi8UCX.gif"
# أيقونة رسائل اللوقز (فوتر لوحة السجلات)
LOG_ICON_URL = "https://i.imgur.com/KRElNgr.gif"

GAMES_CACHE_FILE = "posted_games.txt"

def load_posted_games():
    if os.path.exists(GAMES_CACHE_FILE):
        with open(GAMES_CACHE_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_posted_game(game_id):
    with open(GAMES_CACHE_FILE, "a", encoding="utf-8") as f:
        f.write(f"{game_id}\n")

posted_giveaways = load_posted_games()

# ----------------- قاعدة البيانات -----------------
conn = sqlite3.connect("levels.db", check_same_thread=False)
cursor = conn.cursor()

# قفل واحد مشترك لكل عمليات قاعدة البيانات بالبوت كامل. الـ cursor فوق واحد مشترك بين كل
# الثريدات (لأن كل عمليات القاعدة تشتغل عبر asyncio.to_thread)، وثريدين يستخدمون نفس الـ cursor
# بنفس اللحظة يسببون "sqlite3.ProgrammingError: Recursive use of cursors not allowed".
# قبل هذا التعديل كانت بعض الدوال تستخدم أقفال منفصلة (xp_db_lock, wallet_db_lock, clip_db_lock)
# ودوال ثانية كثيرة كانت بدون أي قفل إطلاقاً — فلو صارت عملية محمية بقفل XP بنفس لحظة عملية
# ثانية غير محمية (أو محمية بقفل مختلف)، تصير المصادمة على نفس الـ cursor وتطيح بالبوت.
# الحل: قفل واحد بس للجميع، يضمن ما يصير أي تعارض بينهم أبداً.
db_lock = threading.Lock()

# ----------------- حفظ بيانات البوت والسكربت قبل التحديث والإغلاق -----------------
SAFETY_BACKUP_DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backups')
SAFETY_BACKUP_KEEP = 24
SAFETY_BACKUP_INTERVAL_MINUTES = 60

def create_safety_backup_sync(reason, command_manifest=None):
    """لقطة SQLite سليمة + السكربت + وصف الأوامر، بدون .env أو التوكن."""
    import json
    import tempfile
    import zipfile
    os.makedirs(SAFETY_BACKUP_DIRECTORY, exist_ok=True)
    stamp=datetime.datetime.now(RIYADH_TZ).strftime('%Y-%m-%d_%H-%M-%S_%f')
    filename=f'bot_backup_{stamp}.zip'
    target=os.path.join(SAFETY_BACKUP_DIRECTORY,filename)
    with tempfile.TemporaryDirectory(prefix='snapshot_',dir=SAFETY_BACKUP_DIRECTORY) as temporary:
        database_path=os.path.join(temporary,'levels.db')
        with db_lock:
            conn.commit()
            snapshot=sqlite3.connect(database_path)
            try:
                conn.backup(snapshot)
                result=snapshot.execute('PRAGMA quick_check').fetchall()
                if result!=[('ok',)]:
                    raise RuntimeError(f'فشل فحص سلامة النسخة: {result}')
            finally:
                snapshot.close()
        script_path=os.path.abspath(__file__)
        manifest={
            'created_at':datetime.datetime.now(RIYADH_TZ).isoformat(),
            'reason':reason,
            'commands':command_manifest or [],
            'commands_list_complete':command_manifest is not None,
            'restore':'Stop the bot, extract main.py and levels.db into the bot directory, then restart. Keep the existing .env and assets.',
        }
        archive_path=os.path.join(temporary,filename)
        with zipfile.ZipFile(archive_path,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(database_path,'levels.db')
            archive.write(script_path,'main.py')
            archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
            archive.writestr('RESTORE.txt','Stop the bot before restoring. Extract main.py and levels.db into the bot directory. Keep .env, images, audio and other assets. Restart the bot to register its slash commands.\n')
        os.replace(archive_path,target)
    # إزالة النسخ الأقدم من مجلد النسخ المحدد فقط.
    snapshots=sorted(name for name in os.listdir(SAFETY_BACKUP_DIRECTORY)
                     if name.startswith('bot_backup_') and name.endswith('.zip') and os.path.isfile(os.path.join(SAFETY_BACKUP_DIRECTORY,name)))
    for old in snapshots[:-SAFETY_BACKUP_KEEP]:
        os.remove(os.path.join(SAFETY_BACKUP_DIRECTORY,old))
    return target

def backup_command_manifest():
    def describe(command):
        result={'name':command.name,'description':getattr(command,'description','')}
        if isinstance(command,app_commands.Group):
            result['commands']=[describe(child) for child in command.commands]
        else:
            result['parameters']=[{'name':parameter.name,'description':parameter.description,'required':parameter.required}
                                  for parameter in getattr(command,'parameters',[])]
        return result
    return [describe(command) for command in bot.tree.get_commands()]

# قبل تشغيل أي تهيئة/ترحيل للجداول نحفظ نسخة من البيانات الموجودة.
try:
    create_safety_backup_sync('قبل تهيئة قاعدة البيانات عند بدء التشغيل')
except Exception as exc:
    print(f'[Safety Backup] تعذر حفظ نسخة بدء التشغيل: {exc}')

cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        guild_id INTEGER,
        user_id INTEGER,
        xp INTEGER DEFAULT 0,
        level INTEGER DEFAULT 0,
        daily_voice_xp INTEGER DEFAULT 0,
        last_voice_date TEXT DEFAULT '',
        PRIMARY KEY (guild_id, user_id)
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS auto_responses (
        guild_id INTEGER,
        trigger_word TEXT,
        response_text TEXT,
        PRIMARY KEY (guild_id, trigger_word)
    )
""")
_auto_response_columns = {row[1] for row in cursor.execute('PRAGMA table_info(auto_responses)').fetchall()}
for _column, _definition in (
    ('match_mode', "TEXT NOT NULL DEFAULT 'word'"),
    ('channel_id', 'INTEGER'),
    ('enabled', 'INTEGER NOT NULL DEFAULT 1'),
):
    if _column not in _auto_response_columns:
        cursor.execute(f'ALTER TABLE auto_responses ADD COLUMN {_column} {_definition}')
cursor.execute("""
    CREATE TABLE IF NOT EXISTS birthdays (
        guild_id INTEGER,
        user_id INTEGER,
        month INTEGER,
        day INTEGER,
        PRIMARY KEY (guild_id, user_id)
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS ticket_counter (
        guild_id INTEGER PRIMARY KEY,
        counter INTEGER DEFAULT 0
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS decree_counter (
        guild_id INTEGER PRIMARY KEY,
        counter INTEGER DEFAULT 0
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS anniversary_log (
        guild_id INTEGER,
        user_id INTEGER,
        year INTEGER,
        kind TEXT,
        PRIMARY KEY (guild_id, user_id, year, kind)
    )
""")
# يسجّل تاريخ منح رتبة المناسبات الخاصة لكل عضو (بتوقيت الرياض)
# عشان آلية الحذف اليومية تشيل الرتبة بس ممن أخذها قبل اليوم، مو من كل حامليها بدون تفريق
cursor.execute("""
    CREATE TABLE IF NOT EXISTS special_role_grants (
        guild_id INTEGER,
        user_id INTEGER,
        granted_date TEXT,
        PRIMARY KEY (guild_id, user_id)
    )
""")
# سجل الإنذارات (warnings) — كل صف إنذار مستقل برقم تسلسلي خاص بكل سيرفر
cursor.execute("""
    CREATE TABLE IF NOT EXISTS warnings (
        guild_id INTEGER,
        warn_id INTEGER,
        user_id INTEGER,
        moderator_id INTEGER,
        reason TEXT,
        timestamp TEXT,
        PRIMARY KEY (guild_id, warn_id)
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS warning_counter (
        guild_id INTEGER PRIMARY KEY,
        counter INTEGER DEFAULT 0
    )
""")
# سجل موحّد لكل الإجراءات الإدارية غير الإنذارات (طرد، حظر، رفع حظر، تايم أوت...)
# يُستخدم مع جدول warnings لبناء سجل كامل بأمر /modlogs
cursor.execute("""
    CREATE TABLE IF NOT EXISTS mod_actions (
        guild_id INTEGER,
        action_id INTEGER,
        user_id INTEGER,
        moderator_id INTEGER,
        action_type TEXT,
        reason TEXT,
        timestamp TEXT,
        PRIMARY KEY (guild_id, action_id)
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS mod_action_counter (
        guild_id INTEGER PRIMARY KEY,
        counter INTEGER DEFAULT 0
    )
""")
# رصيد الدعوات الناجحة لكل عضو (عدد الأعضاء اللي دخلوا بدعوته)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS invite_credits (
        guild_id INTEGER,
        inviter_id INTEGER,
        invite_count INTEGER DEFAULT 0,
        PRIMARY KEY (guild_id, inviter_id)
    )
""")
# رصيد البنك (العملة) لكل عضو
cursor.execute("""
    CREATE TABLE IF NOT EXISTS wallets (
        guild_id INTEGER,
        user_id INTEGER,
        balance INTEGER DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )
""")
# يسجّل آخر شهر/سنة تم دفع الراتب الشهري فيها لكل سيرفر، عشان ما يتكرر الدفع لو البوت أعاد التشغيل بنفس اليوم
cursor.execute("""
    CREATE TABLE IF NOT EXISTS salary_log (
        guild_id INTEGER PRIMARY KEY,
        last_paid_year INTEGER,
        last_paid_month INTEGER
    )
""")
# يسجّل آخر يوم استخدم فيه العضو أمر الروليت اليومي (بتوقيت الرياض) — حتى لا يستخدمه أكثر من مرة باليوم
cursor.execute("""
    CREATE TABLE IF NOT EXISTS roulette_cooldowns (
        guild_id INTEGER,
        user_id INTEGER,
        last_date TEXT,
        PRIMARY KEY (guild_id, user_id)
    )
""")
# يتبّع مبلغ مكافأة الفيديوهات اللي كسبها العضو باليوم (لتطبيق الكاب اليومي VIDEO_REWARD_DAILY_CAP)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS video_reward_daily (
        guild_id INTEGER,
        user_id INTEGER,
        reward_date TEXT,
        earned_today INTEGER DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )
""")
# عناصر متجر العملة: كل عنصر عنده اسم، سعر، صورة (اختيارية) وحالة تفعيل (active) عشان نقدر نسحبه من المتجر لاحقاً
cursor.execute("""
    CREATE TABLE IF NOT EXISTS shop_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER,
        name TEXT,
        price INTEGER,
        image_url TEXT,
        active INTEGER DEFAULT 1
    )
""")
# يسجّل آخر يوم سجّل فيه العضو حضوره اليومي (بتوقيت الرياض) + الستريك الحالي (عدد الأيام المتتالية)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS daily_checkins (
        guild_id INTEGER,
        user_id INTEGER,
        last_date TEXT,
        streak INTEGER DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )
""")
# مسابقة "أفضل لقطة أسبوعية": كل رسالة فيها صورة/فيديو بروم اللقطات تسجّل هنا كـ "مشاركة"
# مرتبطة برسالة التصويت اللي يرسلها البوت تحتها (vote_message_id هو المفتاح اللي نستخدمه وقت الضغط على الزر)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS clip_entries (
        guild_id INTEGER,
        vote_message_id INTEGER,
        entry_message_id INTEGER,
        channel_id INTEGER,
        author_id INTEGER,
        created_ts INTEGER,
        PRIMARY KEY (guild_id, vote_message_id)
    )
""")
# يسجّل كل صوت (عضو صوّت على أي لقطة)، بحيث ما يقدر نفس العضو يصوّت مرتين لنفس اللقطة
cursor.execute("""
    CREATE TABLE IF NOT EXISTS clip_votes (
        guild_id INTEGER,
        vote_message_id INTEGER,
        voter_id INTEGER,
        PRIMARY KEY (guild_id, vote_message_id, voter_id)
    )
""")
# تنظيف الأصوات القديمة غير الصالحة، والاحتفاظ بأول مشاركة لكل مصوّت قبل تطبيق القيد الجديد.
cursor.execute('DELETE FROM clip_votes WHERE NOT EXISTS (SELECT 1 FROM clip_entries e '
               'WHERE e.guild_id=clip_votes.guild_id AND e.vote_message_id=clip_votes.vote_message_id '
               'AND e.author_id<>clip_votes.voter_id)')
cursor.execute('DELETE FROM clip_votes WHERE rowid NOT IN '
               '(SELECT MIN(rowid) FROM clip_votes GROUP BY guild_id, voter_id)')
cursor.execute('DELETE FROM clip_votes WHERE voter_id NOT IN (' +
               ','.join('?' for _ in BEST_CLIP_VOTER_USER_IDS) + ')', BEST_CLIP_VOTER_USER_IDS)
cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS clip_one_vote_per_round ON clip_votes(guild_id, voter_id)')
conn.commit()

# جدول حالة عامة للبوت (يحفظ آخر وقت نشر لقائمة الأغنياء عشان إعادة التشغيل ما تعيد النشر)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS bot_state (
        key TEXT PRIMARY KEY,
        value TEXT
    )
""")
conn.commit()

def get_bot_state(key: str):
    with db_lock:
        cursor.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row[0] if row else None

def set_bot_state(key: str, value: str):
    with db_lock:
        cursor.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )
        conn.commit()

# ----------------- إعدادات حروب البيوت الملكية الأسبوعية -----------------
# روم إعلانات الحرب (توزيع الفرق + النتائج + ليدربورد الفرق) — كلها مع منشن @everyone
WAR_CHANNEL_ID = 1388414282739224727
# الرتب اللي بس أصحابها يدخلون التوزيع على البيوت
WAR_ALLOWED_MEMBER_IDS = {
    1384519174822563965,
    767874265932234763,
    630743401238888478,
    204682468060692480,
    767838210302410772,
    1212149609607209010,
}
WAR_ALLOWED_LEADER_IDS = {
    1384519174822563965,
    767874265932234763,
    630743401238888478,
    204682468060692480,
    767838210302410772,
    1212149609607209010,
}
WAR_TEAM_SIZE = 3            # عدد أعضاء كل بيت
# رتبة لكل بيت: البيت الأول ياخذ الرتبة الأولى، الثاني الثانية، الثالث الثالثة (تنعطى وقت التوزيع وتنسحب لما تنتهي الحرب)
WAR_TEAM_ROLE_IDS = [1554512894358724659, 1554512989686866020, 1554513077519523890]
WAR_MAX_TEAMS = len(WAR_TEAM_ROLE_IDS)  # أقصى عدد بيوت = عدد الرتب (ولازم بيتين كحد أدنى)
# جوائز أول 3 بيوت بالترتيب (تُعطى لكل عضو من أعضاء البيت الفائز — غيّر WAR_PRIZE_PER_MEMBER لو تبيها تنقسم على البيت)
WAR_PRIZES = [100, 50, 25]
WAR_PRIZE_PER_MEMBER = True
# اللقطات (فيديو/صورة بروم اللقطات) تنحسب لبيتك كنقاط إضافية بدون ما تأثر على XP العضو الشخصي
WAR_CLIP_POINTS = 10
WAR_CLIP_DAILY_CAP = 5       # أقصى عدد لقطات تنحسب للعضو باليوم (حماية من السبام)
WAR_NAME_MAX_LEN = 20
# أسماء افتراضية للبيوت لحد ما القائد يختار اسم بيته
WAR_HOUSE_DEFAULTS = [("بيت الصقر", "🛡️"), ("بيت الأسد", "⚔️"), ("بيت النمر", "🔱"), ("بيت الذئب", "🏰")]
# لون جانب كل بيت بالإعلان (بنفس ترتيب رقم البيت)
WAR_HOUSE_COLORS = [(192, 57, 43), (41, 98, 255), (39, 174, 96), (142, 68, 173)]
# لو True: أول حرب تبدأ تلقائياً أول ما يشتغل البوت بهالنظام (بعدها تدور كل جمعة 12 صباحاً). لو False: تبدأ بأمر /royal_war_start
WAR_AUTO_START_FIRST = True

cursor.execute("""
    CREATE TABLE IF NOT EXISTS war_weeks (
        guild_id INTEGER,
        week_id INTEGER,
        started_at TEXT,
        finished_at TEXT,
        status TEXT DEFAULT 'active',
        PRIMARY KEY (guild_id, week_id)
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS war_teams (
        guild_id INTEGER,
        week_id INTEGER,
        team_idx INTEGER,
        name TEXT,
        emoji TEXT,
        leader_id INTEGER,
        named INTEGER DEFAULT 0,
        PRIMARY KEY (guild_id, week_id, team_idx)
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS war_members (
        guild_id INTEGER,
        week_id INTEGER,
        user_id INTEGER,
        team_idx INTEGER,
        PRIMARY KEY (guild_id, week_id, user_id)
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS war_scores (
        guild_id INTEGER,
        week_id INTEGER,
        user_id INTEGER,
        xp_points INTEGER DEFAULT 0,
        clip_points INTEGER DEFAULT 0,
        PRIMARY KEY (guild_id, week_id, user_id)
    )
""")
conn.commit()

war_clip_counts = {}  # (user_id) -> (تاريخ اليوم, عدد اللقطات المحتسبة اليوم)

def _war_active_week_locked(guild_id: int):
    cursor.execute("SELECT week_id FROM war_weeks WHERE guild_id = ? AND status = 'active' ORDER BY week_id DESC LIMIT 1", (guild_id,))
    row = cursor.fetchone()
    return row[0] if row else None

def _war_adjust_points_locked(guild_id: int, user_id: int, delta: int, column: str = "xp_points"):
    """يضيف/يخصم نقاط لبيت العضو بالحرب النشطة. يفترض إن db_lock ممسوك من المستدعي (لا تمسكه هنا أبداً — القفل مو reentrant).
    ما يسوي شي لو العضو مو بأي بيت أو ما فيه حرب نشطة."""
    if not delta or column not in ("xp_points", "clip_points"):
        return
    week_id = _war_active_week_locked(guild_id)
    if week_id is None:
        return
    cursor.execute("SELECT 1 FROM war_members WHERE guild_id = ? AND week_id = ? AND user_id = ?", (guild_id, week_id, user_id))
    if not cursor.fetchone():
        return
    cursor.execute("INSERT OR IGNORE INTO war_scores (guild_id, week_id, user_id) VALUES (?, ?, ?)", (guild_id, week_id, user_id))
    cursor.execute(
        f"UPDATE war_scores SET {column} = MAX(0, {column} + ?) WHERE guild_id = ? AND week_id = ? AND user_id = ?",
        (delta, guild_id, week_id, user_id)
    )

def war_record_clip_sync(guild_id: int, user_id: int, media_count: int) -> int:
    """يحسب نقاط اللقطات لبيت العضو (مع الحد اليومي). يرجّع النقاط المضافة فعلياً."""
    today = datetime.date.today().isoformat()
    prev_day, used = war_clip_counts.get(user_id, (today, 0))
    if prev_day != today:
        used = 0
    allowed = max(0, min(media_count, WAR_CLIP_DAILY_CAP - used))
    if allowed <= 0:
        return 0
    with db_lock:
        week_id = _war_active_week_locked(guild_id)
        if week_id is None:
            return 0
        cursor.execute("SELECT 1 FROM war_members WHERE guild_id = ? AND week_id = ? AND user_id = ?", (guild_id, week_id, user_id))
        if not cursor.fetchone():
            return 0
        points = allowed * WAR_CLIP_POINTS
        _war_adjust_points_locked(guild_id, user_id, points, "clip_points")
        conn.commit()
    war_clip_counts[user_id] = (today, used + allowed)
    return points

def war_get_active_week_sync(guild_id: int):
    with db_lock:
        cursor.execute("SELECT week_id, started_at FROM war_weeks WHERE guild_id = ? AND status = 'active' ORDER BY week_id DESC LIMIT 1", (guild_id,))
        return cursor.fetchone()

def war_get_last_started_sync(guild_id: int):
    """آخر وقت بدأت فيه أي حرب (نشطة أو منتهية) — None لو ما صار ولا حرب."""
    with db_lock:
        cursor.execute("SELECT started_at FROM war_weeks WHERE guild_id = ? ORDER BY week_id DESC LIMIT 1", (guild_id,))
        row = cursor.fetchone()
        return row[0] if row else None

def war_create_week_sync(guild_id: int, started_at: str, teams: list) -> int:
    """teams: [{idx, name, emoji, leader_id, member_ids}] — يحفظ حرب جديدة ويرجّع رقمها."""
    with db_lock:
        cursor.execute("SELECT COALESCE(MAX(week_id), 0) + 1 FROM war_weeks WHERE guild_id = ?", (guild_id,))
        week_id = cursor.fetchone()[0]
        cursor.execute("INSERT INTO war_weeks (guild_id, week_id, started_at, status) VALUES (?, ?, ?, 'active')", (guild_id, week_id, started_at))
        for t in teams:
            cursor.execute(
                "INSERT INTO war_teams (guild_id, week_id, team_idx, name, emoji, leader_id, named) VALUES (?, ?, ?, ?, ?, ?, 0)",
                (guild_id, week_id, t["idx"], t["name"], t["emoji"], t["leader_id"])
            )
            for uid in t["member_ids"]:
                cursor.execute(
                    "INSERT INTO war_members (guild_id, week_id, user_id, team_idx) VALUES (?, ?, ?, ?)",
                    (guild_id, week_id, uid, t["idx"])
                )
        conn.commit()
        return week_id

def war_get_teams_sync(guild_id: int, week_id: int) -> list:
    """يرجّع البيوت مع الأعضاء والنقاط: [{idx, name, emoji, leader_id, named, members:[{id,xp,clip}], score, xp_total, clip_total}]"""
    with db_lock:
        cursor.execute("SELECT team_idx, name, emoji, leader_id, named FROM war_teams WHERE guild_id = ? AND week_id = ? ORDER BY team_idx", (guild_id, week_id))
        team_rows = cursor.fetchall()
        cursor.execute("""
            SELECT m.team_idx, m.user_id, COALESCE(s.xp_points, 0), COALESCE(s.clip_points, 0)
            FROM war_members m
            LEFT JOIN war_scores s ON s.guild_id = m.guild_id AND s.week_id = m.week_id AND s.user_id = m.user_id
            WHERE m.guild_id = ? AND m.week_id = ?
        """, (guild_id, week_id))
        member_rows = cursor.fetchall()
    teams = []
    for idx, name, emoji, leader_id, named in team_rows:
        members = [{"id": uid, "xp": xp, "clip": clip} for (t_idx, uid, xp, clip) in member_rows if t_idx == idx]
        xp_total = sum(m["xp"] for m in members)
        clip_total = sum(m["clip"] for m in members)
        teams.append({
            "idx": idx, "name": name, "emoji": emoji, "leader_id": leader_id, "named": bool(named),
            "members": members, "xp_total": xp_total, "clip_total": clip_total, "score": xp_total + clip_total,
        })
    return teams

def war_get_leader_team_sync(guild_id: int, user_id: int):
    """لو العضو قائد بيت بالحرب النشطة يرجّع (week_id, team_idx, name, named) وإلا None."""
    with db_lock:
        week_id = _war_active_week_locked(guild_id)
        if week_id is None:
            return None
        cursor.execute("SELECT team_idx, name, named FROM war_teams WHERE guild_id = ? AND week_id = ? AND leader_id = ?", (guild_id, week_id, user_id))
        row = cursor.fetchone()
        return (week_id, row[0], row[1], row[2]) if row else None

def war_set_team_name_sync(guild_id: int, week_id: int, team_idx: int, name: str, emoji: str = None) -> str:
    """يسمّي البيت مرة وحدة فقط. يرجّع: 'ok' | 'already' | 'duplicate'."""
    with db_lock:
        cursor.execute("SELECT named FROM war_teams WHERE guild_id = ? AND week_id = ? AND team_idx = ?", (guild_id, week_id, team_idx))
        row = cursor.fetchone()
        if not row or row[0]:
            return "already"
        cursor.execute("SELECT name FROM war_teams WHERE guild_id = ? AND week_id = ? AND team_idx != ?", (guild_id, week_id, team_idx))
        others = {(r[0] or "").strip().lower() for r in cursor.fetchall()}
        if name.strip().lower() in others:
            return "duplicate"
        cursor.execute(
            "UPDATE war_teams SET name = ?, emoji = COALESCE(?, emoji), named = 1 WHERE guild_id = ? AND week_id = ? AND team_idx = ?",
            (name, emoji, guild_id, week_id, team_idx)
        )
        conn.commit()
        return "ok"

def war_finish_week_sync(guild_id: int, week_id: int, finished_at: str):
    with db_lock:
        cursor.execute("UPDATE war_weeks SET status = 'finished', finished_at = ? WHERE guild_id = ? AND week_id = ?", (finished_at, guild_id, week_id))
        conn.commit()

def get_next_ticket_number(guild_id: int) -> int:
    with db_lock:
        cursor.execute("""
            INSERT INTO ticket_counter (guild_id, counter) VALUES (?, 1)
            ON CONFLICT(guild_id) DO UPDATE SET counter = counter + 1
        """, (guild_id,))
        conn.commit()
        cursor.execute("SELECT counter FROM ticket_counter WHERE guild_id = ?", (guild_id,))
        return cursor.fetchone()[0]

def get_next_decree_number(guild_id: int) -> int:
    with db_lock:
        cursor.execute("""
            INSERT INTO decree_counter (guild_id, counter) VALUES (?, 1)
            ON CONFLICT(guild_id) DO UPDATE SET counter = counter + 1
        """, (guild_id,))
        conn.commit()
        cursor.execute("SELECT counter FROM decree_counter WHERE guild_id = ?", (guild_id,))
        return cursor.fetchone()[0]

def record_special_role_grant(guild_id: int, user_id: int, granted_date: str):
    with db_lock:
        cursor.execute("""
            INSERT INTO special_role_grants (guild_id, user_id, granted_date)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET granted_date = ?
        """, (guild_id, user_id, granted_date, granted_date))
        conn.commit()

def get_special_role_grant_date(guild_id: int, user_id: int):
    with db_lock:
        cursor.execute("SELECT granted_date FROM special_role_grants WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        row = cursor.fetchone()
        return row[0] if row else None

def clear_special_role_grant(guild_id: int, user_id: int):
    with db_lock:
        cursor.execute("DELETE FROM special_role_grants WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        conn.commit()

def already_celebrated(guild_id: int, user_id: int, year: int, kind: str) -> bool:
    with db_lock:
        cursor.execute(
            "SELECT 1 FROM anniversary_log WHERE guild_id = ? AND user_id = ? AND year = ? AND kind = ?",
            (guild_id, user_id, year, kind)
        )
        return cursor.fetchone() is not None

def mark_celebrated(guild_id: int, user_id: int, year: int, kind: str):
    with db_lock:
        cursor.execute(
            "INSERT OR IGNORE INTO anniversary_log (guild_id, user_id, year, kind) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, year, kind)
        )
        conn.commit()

# ----------------- دوال نظام الإنذارات (Warnings) -----------------
def add_warning(guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
    """يضيف إنذار جديد ويرجع رقمه التسلسلي داخل السيرفر."""
    with db_lock:
        cursor.execute("""
            INSERT INTO warning_counter (guild_id, counter) VALUES (?, 1)
            ON CONFLICT(guild_id) DO UPDATE SET counter = counter + 1
        """, (guild_id,))
        cursor.execute("SELECT counter FROM warning_counter WHERE guild_id = ?", (guild_id,))
        warn_id = cursor.fetchone()[0]
        timestamp = datetime.datetime.now(RIYADH_TZ).isoformat() if "RIYADH_TZ" in globals() else datetime.datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT INTO warnings (guild_id, warn_id, user_id, moderator_id, reason, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (guild_id, warn_id, user_id, moderator_id, reason, timestamp))
        conn.commit()
        return warn_id

def get_warnings(guild_id: int, user_id: int):
    with db_lock:
        cursor.execute("""
            SELECT warn_id, moderator_id, reason, timestamp FROM warnings
            WHERE guild_id = ? AND user_id = ? ORDER BY warn_id ASC
        """, (guild_id, user_id))
        return cursor.fetchall()

def count_warnings(guild_id: int, user_id: int) -> int:
    with db_lock:
        cursor.execute("SELECT COUNT(*) FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        return cursor.fetchone()[0]

def delete_expired_warnings_sync() -> int:
    """يحذف كل الإنذارات اللي عدت مدة WARNING_EXPIRY_DAYS يوم من تاريخ إصدارها (بكل السيرفرات). يرجع عدد الإنذارات المحذوفة."""
    cutoff = datetime.datetime.now(RIYADH_TZ) - datetime.timedelta(days=WARNING_EXPIRY_DAYS)
    with db_lock:
        cursor.execute("SELECT guild_id, warn_id, timestamp FROM warnings")
        rows = cursor.fetchall()
        expired = []
        for guild_id, warn_id, timestamp in rows:
            try:
                ts = datetime.datetime.fromisoformat(timestamp)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=RIYADH_TZ)
            except Exception:
                continue
            if ts < cutoff:
                expired.append((guild_id, warn_id))
        for guild_id, warn_id in expired:
            cursor.execute("DELETE FROM warnings WHERE guild_id = ? AND warn_id = ?", (guild_id, warn_id))
        conn.commit()
        return len(expired)

async def delete_expired_warnings() -> int:
    return await asyncio.to_thread(delete_expired_warnings_sync)

def remove_warning_by_id(guild_id: int, warn_id: int):
    with db_lock:
        cursor.execute("SELECT user_id FROM warnings WHERE guild_id = ? AND warn_id = ?", (guild_id, warn_id))
        row = cursor.fetchone()
        if not row:
            return None
        cursor.execute("DELETE FROM warnings WHERE guild_id = ? AND warn_id = ?", (guild_id, warn_id))
        conn.commit()
        return row[0]

def clear_warnings(guild_id: int, user_id: int) -> int:
    with db_lock:
        cursor.execute("SELECT COUNT(*) FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        removed = cursor.fetchone()[0]
        cursor.execute("DELETE FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        conn.commit()
        return removed

# ----------------- دوال سجل الإجراءات الإدارية الموحّد (للاستخدام بأمر /modlogs) -----------------
def _get_next_mod_action_id(guild_id: int) -> int:
    with db_lock:
        cursor.execute("""
            INSERT INTO mod_action_counter (guild_id, counter) VALUES (?, 1)
            ON CONFLICT(guild_id) DO UPDATE SET counter = counter + 1
        """, (guild_id,))
        conn.commit()
        cursor.execute("SELECT counter FROM mod_action_counter WHERE guild_id = ?", (guild_id,))
        return cursor.fetchone()[0]

def log_mod_action(guild_id: int, user_id: int, moderator_id, action_type: str, reason: str = None) -> int:
    """يسجّل إجراء إداري (kick / ban / unban / timeout / timeout_remove) بجدول موحّد دائم.
    moderator_id ممكن يكون None لو الفاعل مجهول أو الإجراء تلقائي بالكامل."""
    # ملاحظة: action_id ينحسب بره القفل عمداً لأن _get_next_mod_action_id يمسك db_lock بنفسه،
    # والقفل عندنا مو reentrant (لو مسكناه هنا كمان بنفس الثريد بنعلّق البوت بـ deadlock)
    action_id = _get_next_mod_action_id(guild_id)
    timestamp = datetime.datetime.now(RIYADH_TZ).isoformat() if "RIYADH_TZ" in globals() else datetime.datetime.utcnow().isoformat()
    with db_lock:
        cursor.execute("""
            INSERT INTO mod_actions (guild_id, action_id, user_id, moderator_id, action_type, reason, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (guild_id, action_id, user_id, moderator_id or 0, action_type, reason or "لم يُذكر سبب", timestamp))
        conn.commit()
        return action_id

def get_mod_actions(guild_id: int, user_id: int):
    with db_lock:
        cursor.execute("""
            SELECT action_id, moderator_id, action_type, reason, timestamp FROM mod_actions
            WHERE guild_id = ? AND user_id = ? ORDER BY action_id ASC
        """, (guild_id, user_id))
        return cursor.fetchall()

# ----------------- دوال نظام تتبع الدعوات (Invite Tracker) -----------------
def add_invite_credit(guild_id: int, inviter_id: int):
    with db_lock:
        cursor.execute("""
            INSERT INTO invite_credits (guild_id, inviter_id, invite_count) VALUES (?, ?, 1)
            ON CONFLICT(guild_id, inviter_id) DO UPDATE SET invite_count = invite_count + 1
        """, (guild_id, inviter_id))
        conn.commit()

def get_invite_count(guild_id: int, inviter_id: int) -> int:
    with db_lock:
        cursor.execute("SELECT invite_count FROM invite_credits WHERE guild_id = ? AND inviter_id = ?", (guild_id, inviter_id))
        row = cursor.fetchone()
        return row[0] if row else 0

try:
    cursor.execute("ALTER TABLE users ADD COLUMN daily_voice_xp INTEGER DEFAULT 0")
    cursor.execute("ALTER TABLE users ADD COLUMN last_voice_date TEXT DEFAULT ''")
    conn.commit()
except Exception:
    pass

# عمود XP الأسبوعي — يُستخدم فقط لترتيب/عرض الليدربورد الأسبوعي، ويُصفّر كل جمعة بعد النشر
try:
    cursor.execute("ALTER TABLE users ADD COLUMN weekly_xp INTEGER DEFAULT 0")
    conn.commit()
except Exception:
    pass

text_xp_cooldowns = {}

# قفل يمنع تضارب الكتابة على نفس صف العضو بقاعدة البيانات لما يجي أكثر من مصدر XP بنفس اللحظة
# (مثلاً تكة فويس ورسالة نصية بنفس الثانية) — بدونه ممكن يصير "lost update" ويختل التوافق
# بين الـ XP المخزّن ومستوى العضو (نفس سبب مشكلة "3,952 XP بمستوى 11" اللي طلعت بالصورة)
xp_db_lock = db_lock  # موحّد مع باقي عمليات القاعدة، راجع db_lock بالأعلى

def get_xp_needed(level: int) -> int:
    if level >= MAX_LEVEL:
        return 0
    return int(130 * ((level + 1) ** 1.2))

def get_cumulative_xp(level: int, current_xp: int) -> int:
    """يحسب إجمالي الـ XP الحقيقي المتراكم من أول مستوى (0) لحد اللحظة:
    مجموع XP المطلوب لكل المستويات السابقة (0 إلى level-1) + التقدم الحالي ضمن المستوى الحالي.
    هذا يطابق تماماً منطق الترقية بـ add_user_xp_sync (needed = get_xp_needed(current_lvl))."""
    total = current_xp
    for lvl in range(0, level):
        total += get_xp_needed(lvl)
    return total

def _add_user_xp_locked(guild_id: int, user_id: int, xp_to_add: int, is_voice: bool = False):
    cursor.execute("SELECT xp, level, daily_voice_xp, last_voice_date, weekly_xp FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    row = cursor.fetchone()
    today_str = datetime.date.today().isoformat()

    if not row:
        current_xp = 0
        current_lvl = 0
        daily_voice_xp = 0
        last_voice_date = today_str
        weekly_xp = 0
    else:
        current_xp, current_lvl, daily_voice_xp, last_voice_date, weekly_xp = row
        weekly_xp = weekly_xp or 0

    if current_lvl >= MAX_LEVEL:
        return MAX_LEVEL, False, False, 0, 0

    if last_voice_date != today_str:
        daily_voice_xp = 0
        last_voice_date = today_str

    reached_cap_now = False
    if is_voice:
        if daily_voice_xp >= VOICE_DAILY_CAP:
            return current_lvl, False, False, 0, 0
        if daily_voice_xp + xp_to_add >= VOICE_DAILY_CAP:
            xp_to_add = VOICE_DAILY_CAP - daily_voice_xp
            reached_cap_now = True
        daily_voice_xp += xp_to_add

    current_xp += xp_to_add
    weekly_xp += xp_to_add  # نحسب نفس كمية الـ XP المعتمدة (بعد تطبيق سقف الفويس اليومي) على عداد الأسبوع
    _war_adjust_points_locked(guild_id, user_id, xp_to_add, "xp_points")  # نفس الـ XP يُحسب لبيت العضو بحرب البيوت
    leveled_up = False
    levels_gained = 0

    while current_lvl < MAX_LEVEL:
        needed = get_xp_needed(current_lvl)
        if needed > 0 and current_xp >= needed:
            current_xp -= needed
            current_lvl += 1
            leveled_up = True
            levels_gained += 1
        else:
            break

    if current_lvl >= MAX_LEVEL:
        current_lvl = MAX_LEVEL
        current_xp = 0

    cursor.execute("""
        INSERT INTO users (guild_id, user_id, xp, level, daily_voice_xp, last_voice_date, weekly_xp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) 
        DO UPDATE SET xp = ?, level = ?, daily_voice_xp = ?, last_voice_date = ?, weekly_xp = ?
    """, (guild_id, user_id, current_xp, current_lvl, daily_voice_xp, last_voice_date, weekly_xp,
          current_xp, current_lvl, daily_voice_xp, last_voice_date, weekly_xp))

    return current_lvl, leveled_up, reached_cap_now, levels_gained, xp_to_add

def add_user_xp_sync(guild_id: int, user_id: int, xp_to_add: int, is_voice: bool = False):
    with xp_db_lock:
        try:
            result=_add_user_xp_locked(guild_id,user_id,xp_to_add,is_voice)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise

async def add_user_xp(guild_id: int, user_id: int, xp_to_add: int, is_voice: bool = False, track_quests: bool = False):
    # نشغّل عملية قاعدة البيانات بثريد منفصل حتى لا تعطّل حلقة أحداث البوت
    # (هذي الدالة تُستدعى مع كل رسالة تقريباً، فتعطيلها يأخر كل شي ثاني بالبوت)
    result = await asyncio.to_thread(add_user_xp_sync, guild_id, user_id, xp_to_add, is_voice)
    if track_quests and result[4] > 0:
        guild = bot.get_guild(guild_id)
        member = guild.get_member(user_id) if guild else None
        await bump_quest(guild, member, "weekly_xp", result[4])
    return result[:4]

def reset_weekly_xp_sync(guild_id: int):
    """يصفّر عداد XP الأسبوعي لكل أعضاء السيرفر — يُستدعى بعد نشر الليدربورد الأسبوعي كل جمعة."""
    with xp_db_lock:
        cursor.execute("UPDATE users SET weekly_xp = 0 WHERE guild_id = ?", (guild_id,))
        conn.commit()

async def reset_weekly_xp(guild_id: int):
    return await asyncio.to_thread(reset_weekly_xp_sync, guild_id)

def remove_user_xp_sync(guild_id: int, user_id: int, xp_to_remove: int):
    with xp_db_lock:
        cursor.execute("SELECT xp, level, daily_voice_xp, last_voice_date, weekly_xp FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        row = cursor.fetchone()

        if not row:
            return 0, 0, False

        current_xp, current_lvl, daily_voice_xp, last_voice_date, weekly_xp = row
        weekly_xp = weekly_xp or 0
        old_lvl = current_lvl
        current_xp -= xp_to_remove
        weekly_xp = max(0, weekly_xp - xp_to_remove)
        _war_adjust_points_locked(guild_id, user_id, -xp_to_remove, "xp_points")

        while current_xp < 0 and current_lvl > 0:
            current_lvl -= 1
            needed_prev = get_xp_needed(current_lvl)
            current_xp += needed_prev

        if current_xp < 0 and current_lvl == 0:
            current_xp = 0

        cursor.execute("""
            UPDATE users 
            SET xp = ?, level = ?, weekly_xp = ?
            WHERE guild_id = ? AND user_id = ?
        """, (current_xp, current_lvl, weekly_xp, guild_id, user_id))
        conn.commit()

        level_changed = (current_lvl != old_lvl)
        return current_lvl, current_xp, level_changed

async def remove_user_xp(guild_id: int, user_id: int, xp_to_remove: int):
    return await asyncio.to_thread(remove_user_xp_sync, guild_id, user_id, xp_to_remove)

def normalize_all_users_xp_sync(guild_id: int):
    """
    يفحص كل أعضاء السيرفر ويصحح أي حالة "xp أكبر من المطلوب للفل الحالي" (خلل تراكمي قديم،
    غالبًا بسبب تسابق كتابة على نفس الصف قبل إضافة القفل أعلاه) عن طريق تمرير القيم المخزّنة
    على نفس حلقة الترقية (get_xp_needed) من جديد لحد ما تتوافق مع الفل الصحيح.
    يرجع لائحة [(user_id, old_level, new_level, new_xp), ...] لكل عضو تغيّر وضعه فقط.
    """
    changed = []
    with xp_db_lock:
        cursor.execute("SELECT user_id, xp, level FROM users WHERE guild_id = ?", (guild_id,))
        rows = cursor.fetchall()

        for user_id, xp, level in rows:
            xp = xp or 0
            level = level or 0
            old_level = level
            old_xp = xp
            leveled = False

            while level < MAX_LEVEL:
                needed = get_xp_needed(level)
                if needed > 0 and xp >= needed:
                    xp -= needed
                    level += 1
                    leveled = True
                else:
                    break

            if level >= MAX_LEVEL:
                level = MAX_LEVEL
                xp = 0

            if leveled or xp != old_xp or level != old_level:
                cursor.execute(
                    "UPDATE users SET xp = ?, level = ? WHERE guild_id = ? AND user_id = ?",
                    (xp, level, guild_id, user_id)
                )
                changed.append((user_id, old_level, level, xp))

        conn.commit()
    return changed

async def normalize_all_users_xp(guild_id: int):
    return await asyncio.to_thread(normalize_all_users_xp_sync, guild_id)

def get_user_data_sync(guild_id: int, user_id: int):
    with db_lock:
        cursor.execute("SELECT xp, level, daily_voice_xp, last_voice_date FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        row = cursor.fetchone()
        today_str = datetime.date.today().isoformat()
        if row:
            db_xp, db_lvl, daily_v_xp, last_v_date = row
            if last_v_date != today_str:
                daily_v_xp = 0
                cursor.execute("UPDATE users SET daily_voice_xp = 0, last_voice_date = ? WHERE guild_id = ? AND user_id = ?", (today_str, guild_id, user_id))
                conn.commit()
            return db_xp, db_lvl, daily_v_xp
        return 0, 0, 0

async def get_user_data(guild_id: int, user_id: int):
    return await asyncio.to_thread(get_user_data_sync, guild_id, user_id)

def get_server_rank_sync(guild_id: int, user_id: int) -> int:
    with db_lock:
        cursor.execute("""
            SELECT user_id FROM users 
            WHERE guild_id = ? 
            ORDER BY level DESC, xp DESC
        """, (guild_id,))
        rows = cursor.fetchall()
    for idx, (uid,) in enumerate(rows):
        if uid == user_id:
            return idx + 1
    return 1

async def get_server_rank(guild_id: int, user_id: int) -> int:
    return await asyncio.to_thread(get_server_rank_sync, guild_id, user_id)

def get_member_birthday_sync(guild_id: int, user_id: int):
    with db_lock:
        cursor.execute("SELECT month, day FROM birthdays WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        row = cursor.fetchone()
        return row if row else None

async def get_member_birthday(guild_id: int, user_id: int):
    return await asyncio.to_thread(get_member_birthday_sync, guild_id, user_id)

def get_member_rl_rank_label(member: discord.Member):
    """يرجع اسم رتبة روكت ليق الحالية للعضو إن وجدت، وإلا None."""
    member_role_ids = {r.id for r in member.roles}
    for label, _emoji, role_id, _row in RL_RANKS_CONFIG:
        if role_id != 0 and role_id in member_role_ids:
            return label
    return None

async def update_member_level_role(member: discord.Member, new_level: int):
    guild = member.guild
    role_to_add = None
    target_role_id = LEVEL_ROLES.get(new_level, 0)
    if target_role_id != 0:
        role_to_add = guild.get_role(target_role_id)

    roles_to_remove = []
    for lvl, r_id in LEVEL_ROLES.items():
        if lvl != new_level and r_id != 0:
            role = guild.get_role(r_id)
            if role and role in member.roles:
                roles_to_remove.append(role)

    try:
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="Level role cleanup")
        if role_to_add and role_to_add not in member.roles:
            await member.add_roles(role_to_add, reason=f"Level role assigned (Level {new_level})")
    except discord.Forbidden:
        print("[Error] Missing permissions or Bot role is lower than target roles in role hierarchy!")
    except Exception as e:
        print(f"Role update error for {member.display_name}: {e}")

# ----------------- دوال نظام العملة (البنك + الرواتب الشهرية) -----------------
wallet_db_lock = db_lock  # موحّد مع باقي عمليات القاعدة، راجع db_lock بالأعلى

def get_balance_sync(guild_id: int, user_id: int) -> int:
    with wallet_db_lock:
        cursor.execute("SELECT balance FROM wallets WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        row = cursor.fetchone()
        return row[0] if row else 0

async def get_balance(guild_id: int, user_id: int) -> int:
    return await asyncio.to_thread(get_balance_sync, guild_id, user_id)

def add_balance_sync(guild_id: int, user_id: int, amount: int, reason: str = "تعديل الرصيد", require_funds: bool = False) -> int:
    """يضيف للرصيد (أو يخصم لو amount سالب)، ولا يسمح للرصيد ينزل تحت الصفر. يرجّع الرصيد الجديد."""
    with wallet_db_lock:
        try:
            cursor.execute("SELECT balance FROM wallets WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            row = cursor.fetchone()
            current = row[0] if row else 0
            if require_funds and current + amount < 0:
                return None
            new_balance = max(0, current + amount)
            cursor.execute("""
                INSERT INTO wallets (guild_id, user_id, balance) VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET balance = ?
            """, (guild_id, user_id, new_balance, new_balance))
            record_bank_transaction_locked(guild_id,user_id,new_balance-current,new_balance,reason)
            conn.commit()
            return new_balance
        except Exception:
            conn.rollback()
            raise

async def add_balance(guild_id: int, user_id: int, amount: int, reason: str = "تعديل الرصيد", require_funds: bool = False) -> int:
    return await asyncio.to_thread(add_balance_sync, guild_id, user_id, amount, reason, require_funds)

def add_video_reward_sync(guild_id: int, user_id: int, amount: int) -> int:
    """يحاول يضيف مكافأة فيديو للعضو مع تطبيق الكاب اليومي VIDEO_REWARD_DAILY_CAP.
    يرجّع المبلغ الفعلي اللي انضاف (ممكن يكون أقل من amount أو صفر لو وصل الكاب)."""
    with wallet_db_lock:
        today_str = datetime.datetime.now(RIYADH_TZ).date().isoformat() if "RIYADH_TZ" in globals() else datetime.date.today().isoformat()
        cursor.execute(
            "SELECT reward_date, earned_today FROM video_reward_daily WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id)
        )
        row = cursor.fetchone()
        if not row or row[0] != today_str:
            earned_today = 0
        else:
            earned_today = row[1]

        if earned_today >= VIDEO_REWARD_DAILY_CAP:
            cursor.execute("""
                INSERT INTO video_reward_daily (guild_id, user_id, reward_date, earned_today)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET reward_date = ?, earned_today = ?
            """, (guild_id, user_id, today_str, earned_today, today_str, earned_today))
            conn.commit()
            return 0

        actual_amount = min(amount, VIDEO_REWARD_DAILY_CAP - earned_today)
        earned_today += actual_amount

        cursor.execute("""
            INSERT INTO video_reward_daily (guild_id, user_id, reward_date, earned_today)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET reward_date = ?, earned_today = ?
        """, (guild_id, user_id, today_str, earned_today, today_str, earned_today))
        conn.commit()
        return actual_amount

async def add_video_reward(guild_id: int, user_id: int, amount: int) -> int:
    return await asyncio.to_thread(add_video_reward_sync, guild_id, user_id, amount)

# ----------------- دوال مسابقة "أفضل لقطة أسبوعية" -----------------
clip_db_lock = db_lock  # موحّد مع باقي عمليات القاعدة، راجع db_lock بالأعلى

def add_clip_entry_sync(guild_id: int, vote_message_id: int, entry_message_id: int, channel_id: int, author_id: int):
    """يسجّل مشاركة جديدة بمسابقة أفضل لقطة الأسبوع (تُستدعى فور إرسال رسالة فيها صورة/فيديو بروم اللقطات)."""
    with clip_db_lock:
        cursor.execute("""
            INSERT OR REPLACE INTO clip_entries (guild_id, vote_message_id, entry_message_id, channel_id, author_id, created_ts)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (guild_id, vote_message_id, entry_message_id, channel_id, author_id, int(time.time())))
        conn.commit()

async def add_clip_entry(guild_id: int, vote_message_id: int, entry_message_id: int, channel_id: int, author_id: int):
    await asyncio.to_thread(add_clip_entry_sync, guild_id, vote_message_id, entry_message_id, channel_id, author_id)

# رسائل اللقطات اللي انعطت مكافأة/زر تصويت مسبقاً (حماية من تكرار المكافأة لو الرسالة تعدّلت
# أو لو وصل حدث on_message وon_message_edit لنفس الرسالة). تُصفّر تلقائياً كل إعادة تشغيل للبوت.
processed_clip_message_ids = set()

def _embed_link_domain(embed: discord.Embed) -> str:
    """يرجّع دومين الرابط الأصلي للـ embed (بدون www)، أو فاضي لو ما قدر يحدده."""
    url = embed.url or (embed.video.url if embed.video else None) or (embed.image.url if embed.image else None)
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""

def count_media_in_message(message: discord.Message) -> tuple[int, int]:
    """يحسب عدد الفيديوهات والصور بالرسالة، سواء ملفات مرفوعة مباشرة (attachments) أو روابط
    خارجية (Medal, Streamable, Outplayed...) اللي ديسكورد سوّى لها معاينة (embed) تلقائياً."""
    video_count = sum(
        1 for att in message.attachments
        if (att.content_type and att.content_type.startswith("video/"))
        or att.filename.lower().endswith((".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"))
    )
    photo_count = sum(
        1 for att in message.attachments
        if (att.content_type and att.content_type.startswith("image/"))
        or att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))
    )

    for embed in message.embeds:
        is_video = embed.video is not None or embed.type == "gifv"
        is_image = (not is_video) and (embed.image is not None or embed.type == "image")
        if not is_video and not is_image:
            continue
        if CLIP_LINK_ALLOWED_DOMAINS:
            domain = _embed_link_domain(embed)
            if not any(allowed in domain for allowed in CLIP_LINK_ALLOWED_DOMAINS):
                continue
        if is_video:
            video_count += 1
        else:
            photo_count += 1

    return video_count, photo_count

async def handle_clip_message(message: discord.Message, user_id: int):
    """يتحقق من وجود فيديو/صورة بالرسالة (مرفق أو رابط اتحوّل لمعاينة) ويعطي المكافأة المالية
    + يسجّلها بمسابقة أفضل لقطة أسبوعية + يرسل زر التصويت. تُنادى من on_message مباشرة (للمرفقات)
    ومن on_message_edit (للروابط، لأن ديسكورد يضيف المعاينة بتعديل تلقائي بعد إرسال الرسالة بلحظات)."""
    if message.id in processed_clip_message_ids:
        return

    video_count, photo_count = count_media_in_message(message)
    if video_count == 0 and photo_count == 0:
        return

    processed_clip_message_ids.add(message.id)
    await bump_quest(message.guild, message.author, "weekly_clip", 1)

    # نقاط اللقطة لبيت العضو بحرب البيوت (منفصلة عن المكافأة المالية)
    try:
        await asyncio.to_thread(war_record_clip_sync, message.guild.id, user_id, video_count + photo_count)
    except Exception as e:
        print(f"[Royal War] فشل احتساب نقاط اللقطة: {e}")

    requested_amount = (video_count * VIDEO_REWARD_AMOUNT) + (photo_count * PHOTO_REWARD_AMOUNT)
    if requested_amount > 0:
        try:
            credited_amount = await add_video_reward(message.guild.id, user_id, requested_amount)
            if credited_amount > 0:
                await add_balance(message.guild.id, user_id, credited_amount, reason='مكافأة لقطة')
                announce_channel = bot.get_channel(VIDEO_REWARD_ANNOUNCE_CHANNEL_ID)
                if announce_channel:
                    if video_count > 0 and photo_count > 0:
                        reason_text = "مقابل الفيديو والصور اللي رفعتها"
                    elif video_count > 0:
                        reason_text = "مقابل الفيديو اللي رفعته"
                    else:
                        reason_text = "مقابل الصور اللي رفعتها"
                    reward_embed = discord.Embed(
                        title="🎬 مكافأة لقطات",
                        description=(
                            f"تم إضافة `{credited_amount:,} {CURRENCY_NAME}` {CURRENCY_EMOJI} على حسابك "
                            f"{message.author.mention} {reason_text}!"
                        ),
                        color=discord.Color.from_rgb(0, 168, 252)
                    )
                    apply_branding(reward_embed, message.guild, user=message.author)
                    await announce_channel.send(embed=reward_embed)
            else:
                print(f"[Video Reward] {message.author} ({user_id}) ما أخذ فلوس — وصل الكاب اليومي على الأغلب.")
        except Exception as e:
            print(f"[Video Reward] فشل إعطاء المكافأة لـ {message.author} ({user_id}): {e}")
            traceback.print_exc()

    # ----- تسجيل اللقطة كمشاركة بمسابقة "أفضل لقطة أسبوعية" + إرسال زر التصويت تحتها -----
    try:
        vote_msg = await message.reply(
            clip_vote_rules_text(),
            view=ClipVoteView(),
            mention_author=False
        )
        await add_clip_entry(message.guild.id, vote_msg.id, message.id, message.channel.id, user_id)
    except Exception as e:
        print(f"[Best Clip] تعذّر إرسال رسالة التصويت: {e}")

def get_clip_entry_author_sync(guild_id: int, vote_message_id: int):
    with db_lock:
        cursor.execute(
            "SELECT author_id FROM clip_entries WHERE guild_id = ? AND vote_message_id = ?",
            (guild_id, vote_message_id)
        )
        row = cursor.fetchone()
        return row[0] if row else None

async def get_clip_entry_author(guild_id: int, vote_message_id: int):
    return await asyncio.to_thread(get_clip_entry_author_sync, guild_id, vote_message_id)

def toggle_clip_vote_sync(guild_id: int, vote_message_id: int, voter_id: int) -> str:
    """صوت نهائي واحد لكل عضو في الجولة الأسبوعية؛ التحقق والإضافة تحت قفل واحد."""
    if voter_id not in BEST_CLIP_VOTER_USER_IDS:
        return 'forbidden'
    with clip_db_lock:
        cursor.execute(
            'SELECT author_id FROM clip_entries WHERE guild_id=? AND vote_message_id=?',
            (guild_id, vote_message_id)
        )
        entry = cursor.fetchone()
        if not entry:
            return 'expired'
        if entry[0] == voter_id:
            return 'self'
        cursor.execute('SELECT vote_message_id FROM clip_votes WHERE guild_id=? AND voter_id=?', (guild_id, voter_id))
        previous = cursor.fetchone()
        if previous:
            return 'already' if previous[0] == vote_message_id else 'used'
        cursor.execute('INSERT INTO clip_votes (guild_id, vote_message_id, voter_id) VALUES (?, ?, ?)',
                       (guild_id, vote_message_id, voter_id))
        conn.commit()
        return 'added'

async def toggle_clip_vote(guild_id: int, vote_message_id: int, voter_id: int) -> str:
    return await asyncio.to_thread(toggle_clip_vote_sync, guild_id, vote_message_id, voter_id)

def get_clip_vote_count_sync(guild_id: int, vote_message_id: int) -> int:
    with db_lock:
        cursor.execute(
            "SELECT COUNT(*) FROM clip_votes WHERE guild_id = ? AND vote_message_id = ?",
            (guild_id, vote_message_id)
        )
        row = cursor.fetchone()
        return row[0] if row else 0

async def get_clip_vote_count(guild_id: int, vote_message_id: int) -> int:
    return await asyncio.to_thread(get_clip_vote_count_sync, guild_id, vote_message_id)

def get_weekly_best_clip_sync(guild_id: int):
    """يرجّع بيانات أكثر لقطة تصويتات هذا الأسبوع بهذا السيرفر: (vote_message_id, entry_message_id, channel_id, author_id, vote_count)
    أو None لو ما فيه أي مشاركات أو كلها بدون أصوات."""
    with clip_db_lock:
        cursor.execute("""
            SELECT e.vote_message_id, e.entry_message_id, e.channel_id, e.author_id, COUNT(v.voter_id) AS votes
            FROM clip_entries e
            LEFT JOIN clip_votes v ON v.guild_id = e.guild_id AND v.vote_message_id = e.vote_message_id
            WHERE e.guild_id = ?
            GROUP BY e.vote_message_id
            ORDER BY votes DESC, e.created_ts ASC
            LIMIT 1
        """, (guild_id,))
        row = cursor.fetchone()
        if not row or row[4] < BEST_CLIP_MIN_VOTES:
            return None
        return row

async def get_weekly_best_clip(guild_id: int):
    return await asyncio.to_thread(get_weekly_best_clip_sync, guild_id)

def clear_weekly_clips_sync(guild_id: int):
    """يمسح كل مشاركات وأصوات هذا الأسبوع بعد ما تُحسم النتيجة، عشان الأسبوع الجديد يبدأ من الصفر."""
    with clip_db_lock:
        cursor.execute("DELETE FROM clip_votes WHERE guild_id = ?", (guild_id,))
        cursor.execute("DELETE FROM clip_entries WHERE guild_id = ?", (guild_id,))
        conn.commit()

async def clear_weekly_clips(guild_id: int):
    await asyncio.to_thread(clear_weekly_clips_sync, guild_id)


def get_clip_voters_sync(guild_id, vote_message_id):
    with db_lock:
        return [row[0] for row in conn.execute(
            'SELECT voter_id FROM clip_votes WHERE guild_id=? AND vote_message_id=? ORDER BY voter_id',
            (guild_id, vote_message_id)
        ).fetchall()]


def clip_vote_rules_text():
    return ('🗳️ صوّتوا على هذي اللقطة إذا عجبتكم! صاحب أكثر تصويتات نهاية الأسبوع ياخذ '
            f'`{BEST_CLIP_WEEKLY_REWARD:,} {CURRENCY_NAME}` {CURRENCY_EMOJI}\n'
            'الفوز يحتاج تصويتين على الأقل من الأعضاء المعتمدين. '
            'لكل عضو صوت نهائي واحد بالأسبوع، وما تقدر تصوّت لنفسك.')


def get_active_clip_vote_panels_sync():
    with db_lock:
        return conn.execute('SELECT e.guild_id, e.channel_id, e.vote_message_id, COUNT(v.voter_id) '
                            'FROM clip_entries e LEFT JOIN clip_votes v '
                            'ON v.guild_id=e.guild_id AND v.vote_message_id=e.vote_message_id '
                            'GROUP BY e.guild_id, e.vote_message_id').fetchall()


async def refresh_active_clip_vote_panels():
    for guild_id, channel_id, message_id, count in await asyncio.to_thread(get_active_clip_vote_panels_sync):
        channel = bot.get_channel(channel_id)
        if not channel or channel.guild.id != guild_id:
            continue
        try:
            message = await channel.fetch_message(message_id)
            if message.author.id != bot.user.id:
                continue
            view = ClipVoteView()
            view.children[0].label = f'تصويت ({count})'
            await message.edit(content=clip_vote_rules_text(), view=view)
        except discord.HTTPException as exc:
            print(f'[Clip Panel Refresh] {message_id}: {exc}')

# ----------------- دوال نظام التسجيل اليومي (الحضور اليومي) -----------------
daily_checkin_lock = db_lock  # موحّد مع باقي عمليات القاعدة، راجع db_lock بالأعلى

def record_daily_checkin_sync(guild_id: int, user_id: int):
    """يسجّل حضور العضو لهذا اليوم (بتوقيت الرياض) لو ما سجل من قبل اليوم.
    يرجّع None لو كان مسجّل هذا اليوم مسبقاً (ما نعطيه مكافأة مرتين بنفس اليوم)،
    وإلا يرجّع الستريك الجديد (عدد الأيام المتتالية) بعد التحديث."""
    with daily_checkin_lock:
        today = datetime.datetime.now(RIYADH_TZ).date()
        today_str = today.isoformat()

        cursor.execute(
            "SELECT last_date, streak FROM daily_checkins WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id)
        )
        row = cursor.fetchone()

        if row and row[0] == today_str:
            return None  # سجل حضوره اليوم مسبقاً، ما نكرر المكافأة

        if row and row[0]:
            try:
                last_date = datetime.date.fromisoformat(row[0])
                is_consecutive = (today - last_date).days == 1
            except ValueError:
                is_consecutive = False
            new_streak = (row[1] or 0) + 1 if is_consecutive else 1
        else:
            new_streak = 1

        cursor.execute("""
            INSERT INTO daily_checkins (guild_id, user_id, last_date, streak)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET last_date = ?, streak = ?
        """, (guild_id, user_id, today_str, new_streak, today_str, new_streak))
        conn.commit()
        return new_streak

async def process_daily_checkin(member: discord.Member):
    """يُستدعى عند إرسال رسالة أو دخول روم صوتي لأي عضو — يسجّل حضوره اليومي (مرة وحدة باليوم فقط)،
    يعطيه XP دايم، ويعطيه ريالات بدءاً من ثاني يوم متتالي، وينشر إشعار بروم التسجيل اليومي."""
    if member.bot or not member.guild:
        return

    guild = member.guild
    new_streak = await asyncio.to_thread(record_daily_checkin_sync, guild.id, member.id)
    if new_streak is None:
        return  # سجل حضوره اليوم مسبقاً

    await bump_quest(guild, member, "daily_checkin", 1)

    money_reward = DAILY_CHECKIN_REWARD if new_streak >= 2 else 0
    if money_reward > 0:
        await add_balance(guild.id, member.id, money_reward, reason='مكافأة الحضور اليومي')

    new_level, leveled_up, _, levels_gained = await add_user_xp(guild.id, member.id, DAILY_CHECKIN_XP, is_voice=False, track_quests=True)
    if leveled_up:
        level_reward = levels_gained * LEVEL_UP_REWARD
        if level_reward > 0:
            await add_balance(guild.id, member.id, level_reward, reason='مكافأة ترقية المستوى')
        level_channel = bot.get_channel(LEVEL_CHANNEL_ID)
        if level_channel:
            level_embed = create_levelup_embed(member, new_level, guild, reward=level_reward)
            await level_channel.send(embed=level_embed)
        await update_member_level_role(member, new_level)

    announce_channel = bot.get_channel(DAILY_CHECKIN_ANNOUNCE_CHANNEL_ID)
    if announce_channel:
        if money_reward > 0:
            reward_line = f"`{money_reward} {CURRENCY_NAME}` {CURRENCY_EMOJI} + `{DAILY_CHECKIN_XP} XP`"
        else:
            reward_line = f"`{DAILY_CHECKIN_XP} XP` (بدون ريالات أول تسجيل، تبدأ من ثاني يوم متتالي)"
        checkin_embed = discord.Embed(
            title="📅 تسجيل حضور يومي",
            description=(
                f"سجّل {member.mention} حضوره اليومي!\n\n"
                f"🔥 **الستريك الحالي:** `{new_streak}` يوم متتالي\n"
                f"🎁 **المكافأة:** {reward_line}"
            ),
            color=discord.Color.from_rgb(0, 168, 252)
        )
        apply_branding(checkin_embed, guild, user=member)
        try:
            await announce_channel.send(embed=checkin_embed)
        except Exception as e:
            print(f"Error sending daily checkin announcement: {e}")

def get_last_roulette_date_sync(guild_id: int, user_id: int):
    with wallet_db_lock:
        cursor.execute("SELECT last_date FROM roulette_cooldowns WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        row = cursor.fetchone()
        return row[0] if row else None

async def get_last_roulette_date(guild_id: int, user_id: int):
    return await asyncio.to_thread(get_last_roulette_date_sync, guild_id, user_id)

def set_last_roulette_date_sync(guild_id: int, user_id: int, date_str: str):
    with wallet_db_lock:
        cursor.execute("""
            INSERT INTO roulette_cooldowns (guild_id, user_id, last_date) VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET last_date = ?
        """, (guild_id, user_id, date_str, date_str))
        conn.commit()

async def set_last_roulette_date(guild_id: int, user_id: int, date_str: str):
    await asyncio.to_thread(set_last_roulette_date_sync, guild_id, user_id, date_str)

def was_salary_paid_this_month_sync(guild_id: int, year: int, month: int) -> bool:
    with db_lock:
        cursor.execute("SELECT last_paid_year, last_paid_month FROM salary_log WHERE guild_id = ?", (guild_id,))
        row = cursor.fetchone()
        return bool(row and row[0] == year and row[1] == month)

def mark_salary_paid_sync(guild_id: int, year: int, month: int):
    with db_lock:
        cursor.execute("""
            INSERT INTO salary_log (guild_id, last_paid_year, last_paid_month) VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET last_paid_year = ?, last_paid_month = ?
        """, (guild_id, year, month, year, month))
        conn.commit()

# ----------------- دوال متجر العملة -----------------
def add_shop_item_sync(guild_id: int, name: str, price: int, image_url: str = None) -> int:
    """يضيف عنصر جديد للمتجر ويرجّع رقم العنصر (id) عشان نربطه بزر الشراء."""
    with wallet_db_lock:
        cursor.execute(
            "INSERT INTO shop_items (guild_id, name, price, image_url, active) VALUES (?, ?, ?, ?, 1)",
            (guild_id, name, price, image_url)
        )
        conn.commit()
        return cursor.lastrowid

async def add_shop_item(guild_id: int, name: str, price: int, image_url: str = None) -> int:
    return await asyncio.to_thread(add_shop_item_sync, guild_id, name, price, image_url)

def get_shop_item_sync(item_id: int):
    with wallet_db_lock:
        cursor.execute(
            "SELECT id, guild_id, name, price, image_url, active FROM shop_items WHERE id = ?",
            (item_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {"id": row[0], "guild_id": row[1], "name": row[2], "price": row[3], "image_url": row[4], "active": bool(row[5])}

async def get_shop_item(item_id: int):
    return await asyncio.to_thread(get_shop_item_sync, item_id)

def get_active_shop_items_sync(guild_id: int = None):
    with wallet_db_lock:
        if guild_id is not None:
            cursor.execute(
                "SELECT id, guild_id, name, price, image_url FROM shop_items WHERE active = 1 AND guild_id = ?",
                (guild_id,)
            )
        else:
            cursor.execute("SELECT id, guild_id, name, price, image_url FROM shop_items WHERE active = 1")
        return cursor.fetchall()

async def get_active_shop_items(guild_id: int = None):
    return await asyncio.to_thread(get_active_shop_items_sync, guild_id)

def deactivate_shop_item_sync(item_id: int) -> bool:
    with wallet_db_lock:
        cursor.execute("SELECT id FROM shop_items WHERE id = ?", (item_id,))
        if not cursor.fetchone():
            return False
        cursor.execute("UPDATE shop_items SET active = 0 WHERE id = ?", (item_id,))
        conn.commit()
        return True

async def deactivate_shop_item(item_id: int) -> bool:
    return await asyncio.to_thread(deactivate_shop_item_sync, item_id)


async def create_shop_order_ticket(guild: discord.Guild, member: discord.Member, item_name: str, price: int) -> discord.TextChannel:
    """ينشئ روم تذكرة تلقائي لطلب شراء تم تأكيده، بنفس صلاحيات نظام التذاكر
    (صاحب السيرفر + النائب + الرتب الإضافية + رتب الدعم الفني)، ويحط رقم الطلب واسمه بالإيمبيد."""
    order_number = get_next_ticket_number(guild.id)
    order_tag = f"{order_number:04d}"

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if DEPUTY_ROLE_ID != 0:
        deputy_role = guild.get_role(DEPUTY_ROLE_ID)
        if deputy_role:
            overwrites[deputy_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    for role_id in EXTRA_STAFF_ROLE_IDS:
        if role_id == 0:
            continue
        extra_role = guild.get_role(role_id)
        if extra_role:
            overwrites[extra_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    for role_id in TICKET_SUPPORT_ROLE_IDS[:5]:
        if role_id == 0:
            continue
        support_role = guild.get_role(role_id)
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    if guild.owner:
        overwrites[guild.owner] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    category = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None

    order_channel = await guild.create_text_channel(
        name=f"📦-{order_tag}-{member.name}"[:95],
        category=category,
        overwrites=overwrites,
        topic=f"{member.id}:{order_number}:shop_order",
        reason=f"طلب شراء من المتجر بواسطة {member.name}"
    )

    ticket_channels[order_channel.id] = (member.id, order_number)

    order_embed = discord.Embed(
        title=f"📦 طلب شراء #{order_tag}",
        description=(
            f"أهلاً بك {member.mention}، تم تأكيد عملية شرائك وفتح تذكرة الطلب بنجاح.\n"
            "راح يتم تجهيز طلبك من قبل الإدارة بأقرب وقت ممكن.\n\n"
            "🔒 اضغط الزر بالأسفل لإغلاق التذكرة عند الانتهاء."
        ),
        color=discord.Color.from_rgb(0, 168, 252)
    )
    order_embed.add_field(name="🔢 رقم الطلب", value=f"`#{order_tag}`", inline=True)
    order_embed.add_field(name="🛍️ اسم الطلب", value=item_name, inline=True)
    order_embed.add_field(name="💰 السعر", value=f"`{price:,} {CURRENCY_NAME}`", inline=True)
    order_embed.add_field(name="👤 المشتري", value=member.mention, inline=True)
    apply_branding(order_embed, guild, user=member)

    await order_channel.send(content=member.mention, embed=order_embed, view=TicketControlView())
    return order_channel


class ShopConfirmPurchaseButton(discord.ui.Button):
    """زر تأكيد الشراء الفعلي — يظهر برسالة مؤقتة (ephemeral) بعد الضغط على زر الشراء الأساسي."""
    def __init__(self, item_id: int, name: str, price: int):
        super().__init__(label="تأكيد الشراء", emoji="✅", style=discord.ButtonStyle.success)
        self.item_id = item_id
        self.name = name
        self.price = price

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # نعيد التحقق وقت التأكيد الفعلي (احتياطاً لو تغيّر السعر أو تم إيقاف العنصر أو تغيّر الرصيد بين الوقتين)
        item = await get_shop_item(self.item_id)
        if not item or not item["active"]:
            await interaction.edit_original_response(content="❌ هذا العنصر لم يعد متوفراً بالمتجر حالياً.", embed=None, view=None)
            return

        name, price = item["name"], item["price"]
        guild = interaction.guild
        user_id = interaction.user.id

        balance = await get_balance(guild.id, user_id)
        if balance < price:
            missing = price - balance
            await interaction.edit_original_response(
                content=(
                    f"❌ رصيدك الحالي `{balance:,} {CURRENCY_NAME}` ما يكفي لشراء **{name}** "
                    f"(`{price:,} {CURRENCY_NAME}`).\nتحتاج `{missing:,} {CURRENCY_NAME}` إضافية."
                ),
                embed=None, view=None
            )
            return

        new_balance = await add_balance(guild.id, user_id, -price, reason=f"شراء: {name}", require_funds=True)
        if new_balance is None:
            await interaction.edit_original_response(content="رصيدك تغيّر وما عاد يكفي للشراء.", embed=None, view=None)
            return

        try:
            order_channel = await create_shop_order_ticket(guild, interaction.user, name, price)
            order_line = f"📩 تم فتح تذكرة طلبك تلقائياً: {order_channel.mention}"
        except discord.Forbidden:
            order_channel = None
            order_line = "⚠️ تم خصم المبلغ لكن ما قدر البوت يفتح تذكرة الطلب تلقائياً (صلاحيات ناقصة)، تواصل مع الإدارة."
        except Exception as e:
            order_channel = None
            order_line = f"⚠️ تم خصم المبلغ لكن صار خطأ أثناء فتح تذكرة الطلب: {e}"

        await interaction.edit_original_response(
            content=(
                f"✅ تم شراء **{name}** بنجاح مقابل `{price:,} {CURRENCY_NAME}` {CURRENCY_EMOJI}\n"
                f"💼 رصيدك الحالي: `{new_balance:,} {CURRENCY_NAME}`\n\n"
                f"{order_line}"
            ),
            embed=None, view=None
        )

        log_channel = interaction.client.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(title="🛒 عملية شراء من المتجر", color=discord.Color.green())
            log_embed.add_field(name="👤 المشتري", value=interaction.user.mention, inline=True)
            log_embed.add_field(name="🛍️ العنصر", value=name, inline=True)
            log_embed.add_field(name="💰 السعر", value=f"`{price:,} {CURRENCY_NAME}`", inline=True)
            log_embed.add_field(name="💼 الرصيد بعد الشراء", value=f"`{new_balance:,} {CURRENCY_NAME}`", inline=True)
            if order_channel:
                log_embed.add_field(name="🎫 تذكرة الطلب", value=order_channel.mention, inline=True)
            apply_log_branding(log_embed, guild, target=interaction.user)
            try:
                await log_channel.send(embed=log_embed)
            except Exception as e:
                print(f"[Shop] خطأ بإرسال سجل الشراء: {e}")


class ShopCancelPurchaseButton(discord.ui.Button):
    """زر إلغاء عملية الشراء قبل التأكيد."""
    def __init__(self):
        super().__init__(label="إلغاء", emoji="❌", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="🚫 تم إلغاء عملية الشراء.", embed=None, view=None)


class ShopConfirmPurchaseView(ButtonLimitedView):
    """View مؤقت (غير دائم) يظهر برسالة الإفميرال فقط لتأكيد أو إلغاء الشراء قبل تنفيذه فعلياً."""
    def __init__(self, item_id: int, name: str, price: int):
        super().__init__(timeout=60)
        self.add_item(ShopConfirmPurchaseButton(item_id, name, price))
        self.add_item(ShopCancelPurchaseButton())


class ShopBuyButton(discord.ui.Button):
    """زر شراء أخضر مربوط بعنصر معيّن من المتجر عبر custom_id (شغّال حتى بعد إعادة تشغيل البوت).
    الضغط عليه لا يشتري مباشرة، بل يعرض رسالة تأكيد (زر أخضر للتأكيد وزر رمادي للإلغاء)."""
    def __init__(self, item_id: int, name: str):
        super().__init__(
            label=f"شراء {name}"[:80],
            emoji="🛒",
            style=discord.ButtonStyle.success,  # أخضر
            custom_id=f"shop_buy:{item_id}"
        )
        self.item_id = item_id

    async def callback(self, interaction: discord.Interaction):
        item = await get_shop_item(self.item_id)
        if not item or not item["active"]:
            await interaction.response.send_message("❌ هذا العنصر لم يعد متوفراً بالمتجر حالياً.", ephemeral=True)
            return

        name, price = item["name"], item["price"]
        balance = await get_balance(interaction.guild.id, interaction.user.id)

        confirm_embed = discord.Embed(
            title="🛒 تأكيد الشراء",
            description=(
                f"هل تؤكد شراء **{name}** مقابل `{price:,} {CURRENCY_NAME}` {CURRENCY_EMOJI}؟\n"
                f"💼 رصيدك الحالي: `{balance:,} {CURRENCY_NAME}`"
            ),
            color=discord.Color.from_rgb(0, 168, 252)
        )
        await interaction.response.send_message(
            embed=confirm_embed,
            view=ShopConfirmPurchaseView(self.item_id, name, price),
            ephemeral=True
        )


def build_shop_view(items) -> ButtonLimitedView:
    """يبني View فيها زر شراء أخضر لكل عنصر. items: قائمة من (item_id, name, ...).
    View فيها 25 زر بحد أقصى (حد ديسكورد)، فلو تجاوزنا العدد نتجاهل الباقي بأمان."""
    view = ButtonLimitedView(timeout=None)
    for entry in items[:25]:
        item_id, name = entry[0], entry[1]
        view.add_item(ShopBuyButton(item_id, name))
    return view


def calculate_member_salary(member: discord.Member) -> int:
    """يجمع راتب العضو المباشر ورواتب رتبه المؤهلة."""
    role_ids = {r.id for r in member.roles}
    return SALARY_MEMBERS.get(member.id, 0) + sum(amount for role_id, amount in SALARY_ROLES.items() if role_id in role_ids)

# ----------------- تشكيل الحروف العربية المباشر -----------------
reshaper = arabic_reshaper.ArabicReshaper(configuration={'delete_harakat': True, 'support_ligatures': True})

# مكتبة python-bidi تظبط "اتجاه" الحروف بعد التشكيل (من غيرها الكلام يطلع بترتيب حروف مقلوب/مبعثر بالصور)
# لو مو مثبتة، نرجع لحل بديل بسيط (عكس ترتيب الحروف) لين يتم تثبيتها بالسيرفر: pip install python-bidi
try:
    from bidi.algorithm import get_display as _bidi_get_display
    _HAS_BIDI = True
except ImportError:
    _HAS_BIDI = False
    print("⚠️ مكتبة python-bidi غير مثبتة — النصوص العربية بالصور (العجلة، الترحيب، الليدربورد...) راح تطلع أوضح لو تثبتها: pip install python-bidi")

def reshape_arabic(text: str) -> str:
    try:
        reshaped = reshaper.reshape(str(text))
        if _HAS_BIDI:
            return _bidi_get_display(reshaped)
        # بدون python-bidi: نرجع للتشكيل العادي بس (زي ما كان يشتغل صح من قبل) —
        # عكس ترتيب الحروف يدوياً كان يخربها أكثر، فمو حل بديل آمن
        return reshaped
    except Exception:
        return str(text)

FONT_CACHE = {}

def get_arabic_font(size: int):
    if size in FONT_CACHE:
        return FONT_CACHE[size]

    local_path = "Cairo-Bold.ttf"
    if not os.path.exists(local_path):
        try:
            url = "https://github.com/google/fonts/raw/main/ofl/cairo/Cairo-Bold.ttf"
            urllib.request.urlretrieve(url, local_path)
        except Exception:
            pass

    font_candidates = [
        local_path,
        "arialbd.ttf",
        "arial.ttf",
        "NotoSansArabic-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    for p in font_candidates:
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size)
                FONT_CACHE[size] = f
                return f
            except Exception:
                pass

    return ImageFont.load_default()

# ----------------- رسم عجلة الروليت المتحركة (GIF) -----------------
ROULETTE_WHEEL_SIZE = 380
ROULETTE_SLICE_DEG = 360.0 / len(ROULETTE_SEGMENTS)
ROULETTE_BOUNDS = [
    (i * ROULETTE_SLICE_DEG, (i + 1) * ROULETTE_SLICE_DEG) for i in range(len(ROULETTE_SEGMENTS))
]

def _contrast_text_color(bg_color) -> tuple:
    """يرجّع أبيض أو أسود حسب سطوع لون الخلفية، عشان الكتابة تضل واضحة فوق أي قطعة بالعجلة."""
    r, g, b = bg_color
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return (15, 15, 15) if luminance > 150 else (235, 235, 235)

def _draw_roulette_wheel_frame(rotation_deg: float, size: int = ROULETTE_WHEEL_SIZE) -> Image.Image:
    """يرسم فريم العجلة بدرجات الأزرق وإطار ومؤشر ذهبيين."""
    img = Image.new("RGB", (size, size), (10, 18, 32))
    draw = ImageDraw.Draw(img)
    cx, cy = size / 2, size / 2
    r = size / 2 - 14
    bbox = [cx - r, cy - r, cx + r, cy + r]

    palette = [(26, 62, 107), (36, 100, 169), (38, 78, 133), (48, 133, 205),
               (30, 87, 147), (64, 150, 219), (195, 153, 57), (130, 44, 66)]
    for index, ((label, emoji, kind, value, weight, color), (start, end)) in enumerate(zip(ROULETTE_SEGMENTS, ROULETTE_BOUNDS)):
        color = palette[index % len(palette)]
        draw.pieslice(bbox, start=start + rotation_deg, end=end + rotation_deg, fill=color, outline=(15, 26, 43), width=3)

    font_num = get_arabic_font(30)
    font_txt = get_arabic_font(15)
    for index, ((label, emoji, kind, value, weight, color), (start, end)) in enumerate(zip(ROULETTE_SEGMENTS, ROULETTE_BOUNDS)):
        color = palette[index % len(palette)]
        mid_angle = (start + end) / 2 + rotation_deg
        rad = math.radians(mid_angle)
        is_win = (kind == "win")
        font = font_num if is_win else font_txt
        text = label if is_win else reshape_arabic(label)
        radius_ratio = 0.68 if is_win else 0.62
        tx = cx + math.cos(rad) * r * radius_ratio
        ty = cy + math.sin(rad) * r * radius_ratio
        tb = draw.textbbox((0, 0), text, font=font)
        w, h = tb[2] - tb[0], tb[3] - tb[1]
        draw.text((tx - w / 2, ty - h / 2 - tb[1]), text, font=font, fill=_contrast_text_color(color))

    draw.ellipse(bbox, outline=(210, 171, 85), width=5)
    hub_r = 25
    draw.ellipse([cx - hub_r, cy - hub_r, cx + hub_r, cy + hub_r], fill=(10, 18, 32), outline=(210, 171, 85), width=3)
    draw.text((cx, cy), "B4E", font=get_arabic_font(13), fill=(240, 228, 198), anchor="mm")
    # المؤشر الثابت بالأعلى (يشير دايماً للخانة اللي توقفت عليها العجلة)
    draw.polygon([(cx - 16, 2), (cx + 16, 2), (cx, 34)], fill=(245, 199, 88), outline=(10, 18, 32))
    return img

def _ease_out_cubic(t: float) -> float:
    return 1 - (1 - t) ** 3

def render_roulette_spin_gif_sync(target_label: str) -> io.BytesIO:
    """يولّد GIF لعجلة تدور وتتباطأ تدريجياً لين توقف بالضبط على الخانة target_label."""
    idx = next(i for i, s in enumerate(ROULETTE_SEGMENTS) if s[0] == target_label)
    start_a, end_a = ROULETTE_BOUNDS[idx]
    mid_angle = (start_a + end_a) / 2
    # المؤشر ثابت بالأعلى (زاوية 270 بنظام رسم PIL: 0=يمين، 90=تحت، 180=يسار، 270=فوق)
    base_rotation = (270 - mid_angle) % 360
    spins = 4
    total_rotation = base_rotation + spins * 360

    num_frames = 28
    frames = []
    durations = []
    for i in range(num_frames):
        t = i / (num_frames - 1)
        rot = _ease_out_cubic(t) * total_rotation
        frames.append(_draw_roulette_wheel_frame(rot))
        durations.append(int(40 + 160 * t))  # تباطؤ تدريجي بزيادة مدة كل فريم
    frames.append(_draw_roulette_wheel_frame(total_rotation))
    durations.append(1800)  # وقفة أطول على النتيجة النهائية

    buffer = io.BytesIO()
    frames[0].save(
        buffer, format="GIF", save_all=True, append_images=frames[1:],
        duration=durations, loop=1, optimize=True
    )
    buffer.seek(0)
    return buffer

async def render_roulette_spin_gif(target_label: str) -> io.BytesIO:
    return await asyncio.to_thread(render_roulette_spin_gif_sync, target_label)

def render_roulette_wheel_static_sync() -> io.BytesIO:
    """يرسم صورة ثابتة للعجلة وهي واقفة (بدون دوران) — تُستخدم باللوحة الثابتة عشان العجلة تكون ضاهرة على طول
    بدون ما يحتاج أحد يضغط الزر عشان يشوفها أول مرة."""
    img = _draw_roulette_wheel_frame(0)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

async def render_roulette_wheel_static() -> io.BytesIO:
    return await asyncio.to_thread(render_roulette_wheel_static_sync)

# ----------------- التحقق من الصلاحيات (الأونر أو رتبة النائب) -----------------
class NotAuthorizedError(app_commands.AppCommandError):
    pass

class OwnerOnlyError(app_commands.AppCommandError):
    pass

def is_owner_or_deputy():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        if is_staff_member(interaction.user):
            return True
        raise NotAuthorizedError()
    return app_commands.check(predicate)

def is_owner_or_deputy_or_warn_role():
    """تحقق خاص بأمر /warn فقط: يسمح للأونر والنائب وستاف EXTRA_STAFF_ROLE_IDS (زي is_owner_or_deputy)،
    بالإضافة لأي عضو عنده رتبة WARN_EXTRA_ROLE_ID تحديداً."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        if is_staff_member(interaction.user):
            return True
        role_ids = {r.id for r in interaction.user.roles}
        if WARN_EXTRA_ROLE_ID != 0 and WARN_EXTRA_ROLE_ID in role_ids:
            return True
        raise NotAuthorizedError()
    return app_commands.check(predicate)

def is_owner_only():
    """تحقق يسمح فقط لصاحب السيرفر (القائد) نفسه — لا النائب ولا أي رتبة إدارية أخرى."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        if interaction.user.id == interaction.guild.owner_id:
            return True
        raise OwnerOnlyError()
    return app_commands.check(predicate)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """معالج أخطاء عام لكل أوامر السلاش: يرد على المستخدم برسالة واضحة دائماً،
    ويسجل تفاصيل أي خطأ غير متوقع بروم السجلات بدل ما يضيع بالكونسول فقط."""

    if isinstance(error, OwnerOnlyError) or isinstance(getattr(error, "original", None), OwnerOnlyError):
        msg = "❌ عذراً، هذا الأمر مخصص لصاحب السيرفر (القائد) فقط، حتى النائب ما يقدر يستخدمه!"

    elif isinstance(error, NotAuthorizedError) or isinstance(getattr(error, "original", None), NotAuthorizedError):
        msg = "❌ عذراً، هذا الأمر مخصص لمالك السيرفر أو نائب الإدارة فقط!"

    elif isinstance(error, app_commands.MissingPermissions):
        msg = "❌ ما عندك الصلاحية الكافية لاستخدام هذا الأمر."

    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"⏳ هذا الأمر على كوول داون حالياً، حاول مرة ثانية بعد `{error.retry_after:.1f}` ثانية."

    elif isinstance(error, app_commands.CommandNotFound):
        msg = "❌ هذا الأمر غير موجود، جرب تحدّث قائمة الأوامر."

    else:
        msg = "⚠️ صار خطأ غير متوقع أثناء تنفيذ الأمر، تم إبلاغ الإدارة تلقائياً بالتفاصيل."
        original = getattr(error, "original", error)
        tb_text = "".join(traceback.format_exception(type(original), original, original.__traceback__))
        command_name = interaction.command.name if interaction.command else "غير معروف"
        print(f"[Command Error] /{command_name}:\n{tb_text}")

        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            try:
                error_embed = discord.Embed(title="🐞 خطأ غير متوقع بأمر Slash", color=discord.Color.dark_red())
                error_embed.add_field(name="⌨️ الأمر", value=f"`/{command_name}`", inline=True)
                error_embed.add_field(name="👤 المستخدم", value=interaction.user.mention if interaction.user else "غير معروف", inline=True)
                if interaction.channel:
                    error_embed.add_field(name="📍 الروم", value=interaction.channel.mention, inline=True)
                error_text = tb_text[-1000:] if len(tb_text) > 1000 else tb_text
                error_embed.add_field(name="📄 تفاصيل الخطأ", value=f"```py\n{error_text}\n```", inline=False)
                apply_log_branding(error_embed, interaction.guild, target=interaction.user if isinstance(interaction.user, discord.Member) else None)
                await log_channel.send(embed=error_embed)
            except Exception as log_err:
                print(f"[Error Handler] فشل إرسال تقرير الخطأ لروم السجلات: {log_err}")

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass

# ----------------- نظام أزرار رتب روكت ليق المتكامل -----------------
class RocketLeagueRolesView(ButtonLimitedView):
    def __init__(self):
        super().__init__(timeout=None)
        for label, emoji, role_id, row_idx in RL_RANKS_CONFIG:
            btn = discord.ui.Button(
                label=label,
                emoji=emoji if emoji else None,
                custom_id=f"rl_rank_{label.lower().replace(' ', '_')}",
                style=discord.ButtonStyle.secondary,
                row=row_idx
            )
            btn.callback = self.make_callback(label)
            self.add_item(btn)

    def make_callback(self, rank_label: str):
        async def button_callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            member = interaction.user
            guild = interaction.guild

            all_rl_role_ids = [r_id for _, _, r_id, _ in RL_RANKS_CONFIG if r_id != 0]
            all_rl_roles = [guild.get_role(r_id) for r_id in all_rl_role_ids if guild.get_role(r_id)]

            target_config = next((item for item in RL_RANKS_CONFIG if item[0] == rank_label), None)
            if not target_config or target_config[2] == 0:
                await interaction.followup.send("⚠️ لم يتم تعيين آيدي هذه الرتبة في كود البوت بعد.", ephemeral=True)
                return

            target_role = guild.get_role(target_config[2])
            if not target_role:
                await interaction.followup.send("❌ لم يتم العثور على الرتبة في السيرفر.", ephemeral=True)
                return

            if target_role in member.roles:
                try:
                    await member.remove_roles(target_role, reason="Self-removed Rocket League Rank")
                    embed = discord.Embed(
                        title="🗑️ تم إزالة الرتبة",
                        description=f"تمت إزالة رتبة **{target_role.name}** من حسابك بنجاح.",
                        color=discord.Color.from_rgb(237, 66, 69)
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                except Exception as e:
                    await interaction.followup.send(f"❌ حدث خطأ أثناء إزالة الرتبة: {e}", ephemeral=True)
                return

            roles_to_remove = [r for r in all_rl_roles if r in member.roles]

            try:
                if roles_to_remove:
                    await member.remove_roles(*roles_to_remove, reason="Rocket League rank change")
                await member.add_roles(target_role, reason="Self-assigned Rocket League Rank")
                embed = discord.Embed(
                    title="✅ تم إضافة الرتبة",
                    description=f"تم منحك رتبة **{target_role.name}** بنجاح!",
                    color=discord.Color.from_rgb(87, 242, 135)
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
            except discord.Forbidden:
                await interaction.followup.send("❌ لا يملك البوت صلاحية لإعطائك الرتبة، تأكد من رفع رتبة البوت أعلى من رتب روكت ليق!", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"❌ حدث خطأ: {e}", ephemeral=True)

        return button_callback

# ----------------- واجهة لوحة التحكم المركزية (Control Panel View) مع الأزرار التفاعلية -----------------
class ControlPanelView(ButtonLimitedView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="الرومات الصوتية المؤقتة", emoji="💬", style=discord.ButtonStyle.primary, custom_id="btn_temp_voice", row=0)
    async def temp_voice_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        temp_voice_text = (
            "يمكنك إنشاء غرفتك الخاصة والتحكم بكافة إعداداتها بسهولة.\n\n"
            "📌 **طريقة الإنشاء**\n"
            "انضم إلى روم إنشاء فويس مؤقت وسيتم نقلك مباشرة إلى رومك الخاص.\n\n"
            "🎛️ **أزرار التحكم المباشرة**\n"
            "تظهر لك لوحة أزرار في شات الروم لإدارته بضغطة زر:\n"
            "• 🛡️ **الخصوصية:** قفل الروم أو فتح غرفة الانتظار\n"
            "• 👥 **العدد والاسم:** تعديل اسم الروم وتحديد أقصى عدد للأعضاء\n"
            "• ⛔ **الطرد والحظر:** منع أو طرد أي شخص من الروم\n"
            "• 👤 **الثقة والدعوات:** منح الثقة للأصدقاء وإرسال الدعوات\n"
            "• 👑 **نقل الملكية:** تسليم التحكم لصديق أو حذف الروم\n\n"
            "💡 _يتم حذف الروم الصوتي تلقائياً فور خروج الجميع منه._"
        )
        embed = discord.Embed(title="💬 الرومات الصوتية المؤقتة", description=temp_voice_text, color=discord.Color.from_rgb(87, 242, 135))
        embed.set_image(url=BANNER_IMAGE_URL)
        apply_branding(embed, interaction.guild, user=interaction.user)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="إحصائيات السيرفر (للأدمنز)", emoji="📊", style=discord.ButtonStyle.primary, custom_id="btn_stats", row=1)
    async def stats_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if not guild:
            return

        # التحقق من صلاحيات الأونر أو رتبة النائب أو أي رتبة إضافية
        if not is_staff_member(interaction.user):
            await interaction.response.send_message("❌ صلاحيات مخصصة للاونر والادمنز", ephemeral=True)
            return

        owner_member = guild.owner
        owner_mention = owner_member.mention if owner_member else "غير معروف"
        server_name = guild.name
        server_id = guild.id
        created_at_str = guild.created_at.strftime("%a Jun %d %Y %H:%M:%S GMT")
        total_members = guild.member_count
        boost_tier = guild.premium_tier
        boost_count = guild.premium_subscription_count
        
        default_channel = guild.system_channel.mention if guild.system_channel else "#الشات-العام"
        current_channel = interaction.channel.mention if interaction.channel else "#الإعلانات"
        actor_mention = interaction.user.mention
        user_id = interaction.user.id

        stats_text = (
            f"📊 **معلومات وإحصائيات السيرفر**\n\n"
            f"👑 صاحب السيرفر: {owner_mention}\n"
            f"🏷️ اسم السيرفر: {server_name}\n"
            f"🆔 آيدي السيرفر: {server_id}\n"
            f"📅 تاريخ الإنشاء: {created_at_str}\n"
            f"👥 إجمالي الأعضاء: {total_members}\n"
            f"🚀 مستوى البوست: {boost_tier}\n"
            f"✨ عدد البوستات: {boost_count}\n"
            f"🌐 الروم التلقائي: {default_channel}\n"
            f"💬 القناة الحالية: {current_channel}\n"
            f"👤 منفذ الأمر: {actor_mention}\n"
            f"🆔 آيدي المستخدم: {user_id}"
        )

        embed = discord.Embed(title="معلومات وإحصائيات السيرفر 📊", description=stats_text, color=discord.Color.from_rgb(0, 168, 252))
        embed.set_image(url=BANNER_IMAGE_URL)
        apply_branding(embed, guild, user=interaction.user)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="صلاحيات ومكافآت اللفلات", emoji="🏆", style=discord.ButtonStyle.primary, custom_id="btn_levels", row=1)
    async def levels_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🏆 دليل اللفلات والمميزات",
            description="كل ما زاد نشاطك، ارتفع لفلك وفتحت لك مميزات أكثر.\n"
                        "**مميزات المراحل تراكمية:** كل مرحلة تشمل مميزات المرحلة السابقة.",
            color=discord.Color.from_rgb(241, 196, 15)
        )
        embed.add_field(name="📈 كيف أرفع لفلي؟", value=(
            "• تفاعل في الشات وشارك في الرومات الصوتية.\n"
            "• أكمل مهامك اليومية والأسبوعية للحصول على XP إضافي.\n"
            "• افتح **«رتبتي (Rank)»** لمعرفة خبرتك والخبرة المتبقية للمستوى التالي."
        ), inline=False)
        embed.add_field(name="🥉 البرونزي | لفل 1–9", value=(
            "**التواصل والمشاركة الأساسية**\n"
            "• الكتابة والتحدث، وإرسال الصور والملفات.\n"
            "• الإيموجيات والستيكرات الخارجية.\n"
            "• الكاميرا والبث والساوند بورد."
        ), inline=False)
        embed.add_field(name="🥈 الفضي | لفل 10–25", value=(
            "**مميزات البرونزي +**\n"
            "• تغيير الاسم المستعار داخل السيرفر.\n"
            "• إنشاء الثريدات العامة والخاصة."
        ), inline=False)
        embed.add_field(name="🥇 الذهبي | لفل 26–40", value=(
            "**مميزات الفضي +**\n"
            "• معاينات الروابط وإنشاء الاستطلاعات.\n"
            "• تجاوز وضع التهدئة ومنشن الرتب.\n"
            "• إدارة وإضافة الإيموجيات وتغيير حالة الروم الصوتي."
        ), inline=False)
        embed.add_field(name="💎 البلاتيني | لفل 41–50", value=(
            "**مميزات الذهبي +**\n"
            "• إنشاء وإدارة الفعاليات.\n"
            "• نقل الأعضاء في الرومات الصوتية.\n"
            "• المتحدث ذو الأولوية والاطلاع على سجلات السيرفر."
        ), inline=False)
        embed.add_field(name="🎁 مكافأة الترقية", value=(
            f"تحصل على **{LEVEL_UP_REWARD:,} {CURRENCY_NAME}** {CURRENCY_EMOJI} لكل مستوى جديد.\n"
            "رتبة المستوى تتحدث تلقائيًا عند الترقية."
        ), inline=False)
        embed.add_field(name="🧭 وين أتابع تقدمي؟", value=(
            "**رتبتي (Rank):** اللفل والخبرة المتبقية.\n"
            "**مهامي:** المهام ومكافآتها وتقدمك فيها.\n"
            "**كشف البنك:** رصيدك وسجل معاملاتك."
        ), inline=False)
        embed.set_image(url=BANNER_IMAGE_URL)
        apply_branding(embed, interaction.guild, user=interaction.user)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="الدعم الفني", emoji="🛠️", style=discord.ButtonStyle.primary, custom_id="btn_open_ticket", row=2)
    async def ticket_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user

        # التحقق هل عنده تذكرة مفتوحة أصلاً (نتحقق من التوبيك عشان تبقى الحماية شغالة حتى لو البوت أعاد التشغيل)
        existing = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel) and c.topic and c.topic.split(":")[0] == str(member.id) and c.name.startswith("🎫"),
            guild.text_channels
        )
        if existing:
            await interaction.response.send_message(f"⚠️ عندك تذكرة مفتوحة أصلاً: {existing.mention}", ephemeral=True)
            return

        await interaction.response.send_message(
            "🎫 اختر نوع التذكرة اللي تناسب طلبك من القائمة بالأسفل:",
            view=TicketTypeSelectView(),
            ephemeral=True
        )

    @discord.ui.button(label="بروفايلي", emoji="🪪", style=discord.ButtonStyle.primary, custom_id="btn_profile", row=2)
    async def profile_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await send_profile_embed(interaction, interaction.user)

    @discord.ui.button(label="رتبتي (Rank)", emoji="🏅", style=discord.ButtonStyle.primary, custom_id="btn_rank", row=2)
    async def rank_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """يعرض للعضو بطاقة مستواه ونقاط خبرته كصورة، بنفس منطق أمر /rank بالضبط لكن بشكل خاص (ephemeral)."""
        await interaction.response.defer(ephemeral=True)
        target = interaction.user
        xp, level, daily_v_xp = await get_user_data(interaction.guild.id, target.id)
        needed = get_xp_needed(level)
        rank_pos = await get_server_rank(interaction.guild.id, target.id)
        image_buffer = await create_rank_card(target, level, xp, needed, rank_pos, daily_v_xp)
        discord_file = discord.File(fp=image_buffer, filename="rank.png")
        await interaction.followup.send(file=discord_file, ephemeral=True)

    @discord.ui.button(label="المساعدة", emoji="❓", style=discord.ButtonStyle.primary, custom_id="btn_help", row=2)
    async def help_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_staff = is_staff_member(interaction.user)
        default_topic = HELP_TOPICS["general"]
        embed = discord.Embed(title=default_topic["title"], description=default_topic["text"], color=default_topic["color"])
        embed.set_image(url=BANNER_IMAGE_URL)
        apply_branding(embed, interaction.guild, user=interaction.user)
        await interaction.response.send_message(embed=embed, view=HelpView(is_staff), ephemeral=True)

    @discord.ui.button(label="إرسال اقتراح", emoji="🗳️", style=discord.ButtonStyle.primary, custom_id="btn_suggest", row=3)
    async def suggest_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SuggestionModal())

    @discord.ui.button(label="كشف البنك", emoji="💰", style=discord.ButtonStyle.primary, custom_id="btn_bank", row=3)
    async def bank_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        balance = await get_balance(interaction.guild.id, interaction.user.id)
        embed = discord.Embed(
            title=f"{CURRENCY_EMOJI} كشف البنك",
            description=f"رصيدك الحالي: **{balance:,} {CURRENCY_NAME}**",
            color=discord.Color.from_rgb(241, 196, 15)
        )
        apply_branding(embed, interaction.guild, user=interaction.user)
        await interaction.followup.send(embed=embed, view=MemberBankView(interaction.user.id), ephemeral=True)

    @discord.ui.button(label="تحويل لشخص", emoji="💸", style=discord.ButtonStyle.primary, custom_id="btn_transfer", row=3)
    async def transfer_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        balance = await get_balance(interaction.guild.id, interaction.user.id)
        if balance <= 0:
            await interaction.response.send_message(f"❌ رصيدك الحالي `0 {CURRENCY_NAME}`، ما فيه شي تحوله.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"{CURRENCY_EMOJI} رصيدك الحالي: `{balance:,} {CURRENCY_NAME}`\nاختر الشخص اللي تبي تحول له:",
            view=TransferRecipientView(sender_id=interaction.user.id),
            ephemeral=True
        )

    @discord.ui.button(label="مهامي", emoji="🧭", style=discord.ButtonStyle.primary, custom_id="btn_quests", row=3)
    async def quests_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        embed = await build_quests_embed(interaction.guild, interaction.user)
        await interaction.followup.send(embed=embed, view=PersonalQuestView(interaction.user.id), ephemeral=True)

    @discord.ui.button(label="فريقي", emoji="⚔️", style=discord.ButtonStyle.primary, custom_id="btn_my_team", row=3)
    async def my_team_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_personal_team(interaction)

    @discord.ui.button(label="اختيار الرانك",emoji="🎮",style=discord.ButtonStyle.primary,custom_id="btn_rl_ranks",row=0)
    async def rl_ranks_btn(self,interaction: discord.Interaction,button: discord.ui.Button):
        embed=discord.Embed(
            title="🎮 اختر رانكك في روكت ليق",
            description="اضغط رانكك الحالي ليعطيك البوت رتبته ويزيل رتبة الرانك السابقة. اضغط نفس الرانك مرة ثانية لإزالته.\n\nمتاح Grand Champion I وII وIII. رتبة Supersonic Legend تُنسّق مع الإدارة.",
            color=discord.Color.from_rgb(0,168,252))
        embed.set_thumbnail(url=RL_PANEL_THUMBNAIL_URL)
        apply_branding(embed,interaction.guild,user=interaction.user)
        await interaction.response.send_message(embed=embed,view=RocketLeagueRolesView(),ephemeral=True)

async def create_ticket_channel(interaction: discord.Interaction, type_key: str):
    guild = interaction.guild
    member = interaction.user

    type_info = next((t for t in TICKET_TYPES if t[0] == type_key), None)
    type_label = type_info[1] if type_info else "عام"
    type_emoji = type_info[2] if type_info else "🎫"

    # إعادة التحقق وقت الاختيار الفعلي احتياطاً (لو فتح تذكرة بنافذة ثانية بنفس الوقت)
    existing = discord.utils.find(
        lambda c: isinstance(c, discord.TextChannel) and c.topic and c.topic.split(":")[0] == str(member.id) and c.name.startswith("🎫"),
        guild.text_channels
    )
    if existing:
        await interaction.followup.send(f"⚠️ عندك تذكرة مفتوحة أصلاً: {existing.mention}", ephemeral=True)
        return

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if DEPUTY_ROLE_ID != 0:
        deputy_role = guild.get_role(DEPUTY_ROLE_ID)
        if deputy_role:
            overwrites[deputy_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        else:
            print(f"[Tickets] ⚠️ تحذير: ما لقيت رتبة النائب بالآيدي {DEPUTY_ROLE_ID} — تأكد إنها موجودة وما تغيّر آيديها.")
    for role_id in EXTRA_STAFF_ROLE_IDS:
        if role_id == 0:
            continue
        extra_role = guild.get_role(role_id)
        if extra_role:
            overwrites[extra_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    for role_id in TICKET_SUPPORT_ROLE_IDS[:5]:
        if role_id == 0:
            continue
        support_role = guild.get_role(role_id)
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        else:
            print(f"[Tickets] ⚠️ تحذير: ما لقيت رتبة دعم بالآيدي {role_id} — تأكد إنها موجودة وما تغيّر آيديها.")
    if guild.owner:
        overwrites[guild.owner] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    category = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
    ticket_number = get_next_ticket_number(guild.id)
    ticket_tag = f"{ticket_number:04d}"

    try:
        ticket_channel = await guild.create_text_channel(
            name=f"🎫-{ticket_tag}-{member.name}"[:95],
            category=category,
            overwrites=overwrites,
            topic=f"{member.id}:{ticket_number}:{type_key}",
            reason=f"فتح تذكرة دعم فني بواسطة {member.name}"
        )
    except discord.Forbidden:
        await interaction.followup.send("❌ لا يملك البوت صلاحية إنشاء رومات، تأكد من صلاحياته.", ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(f"❌ حدث خطأ أثناء فتح التذكرة: {e}", ephemeral=True)
        return

    ticket_channels[ticket_channel.id] = (member.id, ticket_number)

    ticket_embed = discord.Embed(
        title=f"{type_emoji} تذكرة {type_label} #{ticket_tag}",
        description=(
            f"أهلاً بك {member.mention}، تم فتح تذكرتك بنجاح.\n"
            "اشرح مشكلتك أو استفسارك بالتفصيل وراح يتم الرد عليك من الإدارة بأقرب وقت.\n\n"
            "🔒 اضغط الزر بالأسفل لإغلاق التذكرة عند الانتهاء."
        ),
        color=discord.Color.from_rgb(237, 66, 69)
    )
    ticket_embed.add_field(name="🏷️ نوع التذكرة", value=type_label, inline=True)
    apply_branding(ticket_embed, guild, user=member)
    await ticket_channel.send(content=member.mention, embed=ticket_embed, view=TicketControlView())
    await interaction.followup.send(f"✅ تم فتح تذكرتك رقم **#{ticket_tag}** بنجاح: {ticket_channel.mention}", ephemeral=True)

class TicketTypeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=label, description=desc, emoji=emoji, value=key)
            for key, label, emoji, desc in TICKET_TYPES
        ]
        super().__init__(
            placeholder="اختر نوع التذكرة...",
            options=options,
            min_values=1,
            max_values=1,
            custom_id="select_ticket_type"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await create_ticket_channel(interaction, self.values[0])

class TicketTypeSelectView(ButtonLimitedView):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketTypeSelect())

def can_contact_ticket_staff(interaction):
    channel = interaction.channel
    if not interaction.guild or not channel:
        return False
    opener_id, _ = ticket_channels.get(channel.id, (None, None))
    topic = getattr(channel, 'topic', None)
    if opener_id is None and topic:
        owner = topic.split(':')[0]
        if owner.isdigit():
            opener_id = int(owner)
    return opener_id is not None and (
        interaction.user.id == opener_id
        or is_staff_member(interaction.user)
        or any(role.id in TICKET_SUPPORT_ROLE_IDS[:5] for role in interaction.user.roles)
    )

class TicketStaffContactSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder='اختر الشخص اللي تبي تناديه', options=[
            discord.SelectOption(label='منشن القائد', value='owner', emoji='👑'),
            discord.SelectOption(label='منشن النائب', value='deputy', emoji='🛡️'),
        ])

    async def callback(self, interaction: discord.Interaction):
        if not can_contact_ticket_staff(interaction):
            await interaction.response.send_message('ما تملك صلاحية طلب الإدارة في هذي التذكرة.', ephemeral=True)
            return
        guild = interaction.guild
        if self.values[0] == 'owner':
            mention = f'<@{guild.owner_id}>'
            allowed = discord.AllowedMentions(everyone=False, users=[discord.Object(id=guild.owner_id)], roles=False, replied_user=False)
        else:
            deputy = guild.get_role(DEPUTY_ROLE_ID)
            if deputy is None:
                await interaction.response.send_message('رتبة النائب غير موجودة بالسيرفر.', ephemeral=True)
                return
            mention = deputy.mention
            allowed = discord.AllowedMentions(everyone=False, users=False, roles=[deputy], replied_user=False)
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.channel.send(
                f'📣 {mention}\nصاحب الطلب: {interaction.user.mention} — يحتاج مساعدتك في هذي التذكرة.',
                allowed_mentions=allowed)
        except discord.HTTPException:
            await interaction.edit_original_response(content='تعذر إرسال المنشن؛ حاول مرة ثانية.', view=None)
            return
        await interaction.edit_original_response(content='✅ تم إرسال المنشن داخل التذكرة.', view=None)

class TicketStaffContactView(ButtonLimitedView):
    def __init__(self, requester_id):
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.add_item(TicketStaffContactSelect())

    async def interaction_check(self, interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message('افتح قائمة طلب الإدارة من زر التذكرة.', ephemeral=True)
            return False
        return True

class TicketControlView(ButtonLimitedView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='طلب الإدارة', emoji='📣', style=discord.ButtonStyle.primary, custom_id='btn_ticket_contact_staff')
    async def contact_staff_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not can_contact_ticket_staff(interaction):
            await interaction.response.send_message('ما تملك صلاحية طلب الإدارة في هذي التذكرة.', ephemeral=True)
            return
        await interaction.response.send_message(
            'اختر منشن القائد أو النائب؛ المنشن يُرسل داخل هذي التذكرة.',
            view=TicketStaffContactView(interaction.user.id), ephemeral=True)

    @discord.ui.button(label="إغلاق التذكرة", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="btn_close_ticket")
    async def close_ticket_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        guild = interaction.guild

        opener_id, ticket_number = ticket_channels.get(channel.id, (None, None))
        type_key = None
        if channel.topic:
            parts = channel.topic.split(":")
            if opener_id is None and parts and parts[0].isdigit():
                opener_id = int(parts[0])
            if ticket_number is None and len(parts) > 1 and parts[1].isdigit():
                ticket_number = int(parts[1])
            if len(parts) > 2:
                type_key = parts[2]

        is_staff = is_staff_member(interaction.user)
        is_support = any(r.id in TICKET_SUPPORT_ROLE_IDS[:5] for r in interaction.user.roles)
        is_ticket_owner = (interaction.user.id == opener_id)

        if not (is_staff or is_support or is_ticket_owner):
            await interaction.response.send_message("❌ ما تملك صلاحية إغلاق هذي التذكرة.", ephemeral=True)
            return

        await interaction.response.send_message("🔒 جاري إغلاق التذكرة...", ephemeral=False)

        ticket_tag = f"{ticket_number:04d}" if ticket_number else "غير معروف"

        try:
            def clean_content(msg: discord.Message) -> str:
                content = msg.content or "*مرفق/وسائط بدون نص*"
                for u in msg.mentions:
                    content = content.replace(f"<@{u.id}>", f"@{u.display_name}").replace(f"<@!{u.id}>", f"@{u.display_name}")
                return content

            embed_lines = []
            async for msg in channel.history(limit=500, oldest_first=True):
                time_str = msg.created_at.astimezone(RIYADH_TZ).strftime("%Y-%m-%d %I:%M %p")
                content = clean_content(msg)
                embed_lines.append(f"**{msg.author.display_name}** • `{time_str}`\n{content}")

            preview_text = "\n\n".join(embed_lines) if embed_lines else "لا يوجد أي رسائل بهذي التذكرة."
            truncated = False
            if len(preview_text) > 4000:
                preview_text = preview_text[:4000]
                truncated = True

            log_channel = interaction.client.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                opener_member = guild.get_member(opener_id) if opener_id else None
                log_embed = discord.Embed(
                    title=f"🎫 تم إغلاق التذكرة #{ticket_tag}",
                    description=preview_text + ("\n\n*(المعاينة مقتطعة، شوف الملف المرفق للنسخة الكاملة)*" if truncated else ""),
                    color=discord.Color.dark_grey()
                )
                log_embed.add_field(name="🔢 رقم التذكرة", value=f"`#{ticket_tag}`", inline=True)
                type_info = next((t for t in TICKET_TYPES if t[0] == type_key), None)
                if type_info:
                    log_embed.add_field(name="🏷️ نوع التذكرة", value=f"{type_info[2]} {type_info[1]}", inline=True)
                log_embed.add_field(name="👤 صاحب التذكرة", value=opener_member.mention if opener_member else f"`{opener_id}`", inline=True)
                log_embed.add_field(name="🔒 أُغلقت بواسطة", value=interaction.user.mention, inline=True)
                apply_log_branding(log_embed, guild, target=opener_member or interaction.user)

                # نرفق دايماً ملف نصي بكامل المحادثة (حتى لو ما تجاوزت حد المعاينة) عشان أرشيف كامل موثوق
                full_transcript = "\n\n".join(embed_lines) if embed_lines else "لا يوجد أي رسائل بهذي التذكرة."
                transcript_file = discord.File(
                    fp=io.BytesIO(full_transcript.encode("utf-8")),
                    filename=f"transcript_ticket_{ticket_tag}.txt"
                )
                await log_channel.send(embed=log_embed, file=transcript_file)
        except Exception as e:
            print(f"[Tickets] خطأ أثناء حفظ نسخة التذكرة: {e}")

        ticket_channels.pop(channel.id, None)
        await asyncio.sleep(3)
        try:
            await channel.delete(reason=f"تم إغلاق التذكرة بواسطة {interaction.user}")
        except Exception as e:
            print(f"[Tickets] خطأ أثناء حذف روم التذكرة: {e}")

# ----------------- رسم صورة واجهة التحكم بالفويس المؤقت -----------------
def render_temp_voice_guide_sync() -> io.BytesIO:
    if os.path.exists("voice_controls.png"):
        with open("voice_controls.png", "rb") as f:
            return io.BytesIO(f.read())

    # نفس فكرة لوحة القائد: 4 أعمدة بدل 5 حتى تكون الصورة أضيق ومنظمة بشكل متساوي
    width, height = 620, 252
    img = Image.new("RGBA", (width, height), (30, 31, 34, 255))
    draw = ImageDraw.Draw(img)

    font = get_arabic_font(15)

    buttons_layout = [
        ["الدردشة", "الانتظار", "قفل", "إخفاء"],
        ["الاسم", "الحد", "السيرفر", "ثقة"],
        ["طرد", "دعوة", "فك حظر", "حظر"],
        ["حذف", "نقل الملكية", "استلام", "الحالة"],
    ]

    card_w, card_h = 136, 48
    margin_x, margin_y = 15, 15
    gap_x, gap_y = 10, 10

    for r_idx, row in enumerate(buttons_layout):
        for c_idx, label in enumerate(row):
            x = margin_x + c_idx * (card_w + gap_x)
            y = margin_y + r_idx * (card_h + gap_y)

            bg_color = (218, 55, 60, 255) if label in ("حذف", "حظر") else (43, 45, 49, 255)
            draw.rounded_rectangle([x, y, x + card_w, y + card_h], radius=8, fill=bg_color)

            text_ar = reshape_arabic(label)
            draw.text((x + card_w // 2, y + card_h // 2), text_ar, fill=(240, 240, 240), font=font, anchor="mm")

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

async def create_temp_voice_guide_image() -> io.BytesIO:
    return await asyncio.to_thread(render_temp_voice_guide_sync)

# ----------------- صورة شرح أزرار لوحة القائد والنائب (رومات ثابتة) -----------------
def render_leader_voice_guide_sync() -> io.BytesIO:
    # نفس فكرة صورة الفويس المؤقت: لو حاط صورة جاهزة (أضيق ومصممة يدويًا) بجانب main.py
    # باسم leader_voice_controls.png، البوت يستخدمها مباشرة بدل ما يرسم صورة تلقائية
    if os.path.exists("leader_voice_controls.png"):
        with open("leader_voice_controls.png", "rb") as f:
            return io.BytesIO(f.read())

    width, height = 620, 252
    img = Image.new("RGBA", (width, height), (30, 31, 34, 255))
    draw = ImageDraw.Draw(img)

    font = get_arabic_font(15)

    buttons_layout = [
        ["الدردشة", "الانتظار", "قفل", "إخفاء"],
        ["الاسم", "الحد", "السيرفر", "الثقة"],
        ["طرد", "دعوة", "حظر", "فك حظر"],
        ["استدعاء", "خطاب ملكي", "مرسوم", "مساعدة"],
    ]

    card_w, card_h = 136, 48
    margin_x, margin_y = 15, 15
    gap_x, gap_y = 10, 10

    for r_idx, row in enumerate(buttons_layout):
        for c_idx, label in enumerate(row):
            x = margin_x + c_idx * (card_w + gap_x)
            y = margin_y + r_idx * (card_h + gap_y)

            bg_color = (218, 55, 60, 255) if label == "حظر" else (43, 45, 49, 255)
            draw.rounded_rectangle([x, y, x + card_w, y + card_h], radius=8, fill=bg_color)

            text_ar = reshape_arabic(label)
            draw.text((x + card_w // 2, y + card_h // 2), text_ar, fill=(240, 240, 240), font=font, anchor="mm")

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

async def create_leader_voice_guide_image() -> io.BytesIO:
    return await asyncio.to_thread(render_leader_voice_guide_sync)

# ----------------- نظام اقتراحات الأعضاء (Suggestions) -----------------
# ----------------- حالات الاقتراحات والتصويتات المحفوظة -----------------
SUGGESTION_STATES = {
    'pending':('🗳️ اقتراح جديد','جديد',(0,168,252)),
    'reviewing':('🔎 اقتراح قيد الدراسة','قيد الدراسة',(241,196,15)),
    'accepted':('✅ اقتراح مقبول','مقبول',(87,242,135)),
    'implemented':('🛠️ تم تنفيذ الاقتراح','تم التنفيذ',(46,204,113)),
    'rejected':('❌ اقتراح مرفوض','مرفوض',(237,66,69)),
}
SUGGESTION_TRANSITIONS = {
    'pending':{'reviewing','accepted','rejected'},
    'reviewing':{'accepted','rejected'},
    'accepted':{'implemented','reviewing','rejected'},
    'rejected':{'reviewing','accepted'},
    'implemented':set(),
}
cursor.executescript("""
CREATE TABLE IF NOT EXISTS member_suggestions (
    message_id INTEGER PRIMARY KEY,guild_id INTEGER,channel_id INTEGER,author_id INTEGER,
    content TEXT,status TEXT DEFAULT 'pending',reviewer_id INTEGER,updated_at REAL,
    revision INTEGER DEFAULT 0,rendered_revision INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS suggestion_ballots (
    message_id INTEGER,user_id INTEGER,side TEXT,
    PRIMARY KEY(message_id,user_id)
);
CREATE TABLE IF NOT EXISTS suggestion_status_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,message_id INTEGER,status TEXT,reviewer_id INTEGER,
    created_at REAL,notified INTEGER DEFAULT 0
);
""")
conn.commit()
suggestion_action_lock=asyncio.Lock()

def save_suggestion_sync(message_id,guild_id,channel_id,author_id,content):
    with db_lock:
        cursor.execute("INSERT INTO member_suggestions(message_id,guild_id,channel_id,author_id,content,updated_at) VALUES (?, ?, ?, ?, ?, ?)",(message_id,guild_id,channel_id,author_id,content,time.time()))
        conn.commit()

def suggestion_data_sync(message_id):
    with db_lock:
        cursor.execute("SELECT guild_id,channel_id,author_id,content,status,reviewer_id,updated_at,revision FROM member_suggestions WHERE message_id=?",(message_id,))
        row=cursor.fetchone()
        if not row:
            return None
        cursor.execute("SELECT side,COUNT(*) FROM suggestion_ballots WHERE message_id=? GROUP BY side",(message_id,))
        counts=dict(cursor.fetchall())
    keys=('guild_id','channel_id','author_id','content','status','reviewer_id','updated_at','revision')
    return {**dict(zip(keys,row)),'up':counts.get('up',0),'down':counts.get('down',0)}

def toggle_suggestion_vote_sync(message_id,guild_id,user_id,side):
    if side not in ('up','down'):
        raise ValueError('Unknown vote')
    with db_lock:
        try:
            cursor.execute("SELECT status,guild_id FROM member_suggestions WHERE message_id=?",(message_id,))
            row=cursor.fetchone()
            if not row or row[1]!=guild_id:
                return False,'هذا اقتراح قديم غير مسجل بالنظام الجديد.'
            if row[0] not in ('pending','reviewing'):
                return False,'التصويت مقفل بعد اتخاذ القرار.'
            cursor.execute("SELECT side FROM suggestion_ballots WHERE message_id=? AND user_id=?",(message_id,user_id))
            old=cursor.fetchone()
            if old and old[0]==side:
                cursor.execute("DELETE FROM suggestion_ballots WHERE message_id=? AND user_id=?",(message_id,user_id))
            else:
                cursor.execute("INSERT OR REPLACE INTO suggestion_ballots VALUES (?, ?, ?)",(message_id,user_id,side))
            cursor.execute("UPDATE member_suggestions SET revision=revision+1 WHERE message_id=?",(message_id,))
            conn.commit()
            return True,None
        except Exception:
            conn.rollback()
            raise

def change_suggestion_status_sync(message_id,guild_id,reviewer_id,status):
    with db_lock:
        try:
            cursor.execute("SELECT status,guild_id FROM member_suggestions WHERE message_id=?",(message_id,))
            row=cursor.fetchone()
            if not row or row[1]!=guild_id:
                return False,'هذا اقتراح قديم غير مسجل بالنظام الجديد.'
            current=row[0]
            if current==status:
                return False,'الاقتراح بهذه الحالة بالفعل.'
            if status not in SUGGESTION_TRANSITIONS.get(current,set()):
                return False,'لا يمكن تنفيذ هذا الانتقال؛ تم التنفيذ يتطلب قبول الاقتراح أولًا.'
            now=time.time()
            cursor.execute("UPDATE member_suggestions SET status=?,reviewer_id=?,updated_at=?,revision=revision+1 WHERE message_id=?",(status,reviewer_id,now,message_id))
            cursor.execute("INSERT INTO suggestion_status_updates(message_id,status,reviewer_id,created_at) VALUES (?, ?, ?, ?)",(message_id,status,reviewer_id,now))
            conn.commit()
            return True,None
        except Exception:
            conn.rollback()
            raise

def mark_suggestion_rendered_sync(message_id,revision):
    with db_lock:
        cursor.execute("UPDATE member_suggestions SET rendered_revision=? WHERE message_id=?",(revision,message_id))
        conn.commit()

def suggestion_render_queue_sync():
    with db_lock:
        cursor.execute("SELECT message_id,guild_id,channel_id FROM member_suggestions WHERE rendered_revision<revision")
        return cursor.fetchall()

def suggestion_notification_queue_sync():
    with db_lock:
        cursor.execute("""SELECT u.id,s.message_id,s.guild_id,s.channel_id,s.author_id,s.content,u.status,u.reviewer_id,u.created_at
            FROM suggestion_status_updates u JOIN member_suggestions s ON s.message_id=u.message_id
            WHERE u.notified=0 ORDER BY u.id""")
        return cursor.fetchall()

def mark_suggestion_update_notified_sync(update_id):
    with db_lock:
        cursor.execute("UPDATE suggestion_status_updates SET notified=1 WHERE id=?",(update_id,))
        conn.commit()

def apply_suggestion_state_to_embed(embed,data):
    title,label,color=SUGGESTION_STATES[data['status']]
    embed.title=title
    embed.color=discord.Color.from_rgb(*color)
    embed.description=data['content']
    embed.clear_fields()
    embed.add_field(name='👍 مع',value=str(data['up']),inline=True)
    embed.add_field(name='👎 ضد',value=str(data['down']),inline=True)
    embed.add_field(name='الحالة',value=label,inline=True)
    embed.add_field(name='صاحب الاقتراح',value=f"<@{data['author_id']}>",inline=True)
    if data['reviewer_id']:
        embed.add_field(name='آخر تحديث',value=f"<@{data['reviewer_id']}> • <t:{int(data['updated_at'])}:f>",inline=False)
    return embed

async def refresh_suggestion_message(message,data):
    embed=message.embeds[0].copy() if message.embeds else discord.Embed()
    apply_suggestion_state_to_embed(embed,data)
    await message.edit(embed=embed,view=SuggestionVoteView(data['status']),allowed_mentions=discord.AllowedMentions.none())
    await asyncio.to_thread(mark_suggestion_rendered_sync,message.id,data['revision'])

@tasks.loop(minutes=1)
async def suggestion_workflow_task():
    async with suggestion_action_lock:
        for message_id,guild_id,channel_id in await asyncio.to_thread(suggestion_render_queue_sync):
            guild=bot.get_guild(guild_id)
            if not guild:
                continue
            try:
                channel=guild.get_channel(channel_id) or await guild.fetch_channel(channel_id)
                message=await channel.fetch_message(message_id)
                data=await asyncio.to_thread(suggestion_data_sync,message_id)
                await refresh_suggestion_message(message,data)
            except discord.NotFound:
                data=await asyncio.to_thread(suggestion_data_sync,message_id)
                if data:
                    await asyncio.to_thread(mark_suggestion_rendered_sync,message_id,data['revision'])
            except discord.HTTPException as exc:
                print(f'[Suggestions] تعذر تحديث رسالة {message_id}: {exc}')
    for update_id,message_id,guild_id,channel_id,author_id,content,status,reviewer_id,created_at in await asyncio.to_thread(suggestion_notification_queue_sync):
        guild=bot.get_guild(guild_id)
        if not guild:
            continue
        try:
            preferences=await asyncio.to_thread(member_preferences_sync,guild_id,author_id)
            if not preferences['suggestions']:
                await asyncio.to_thread(mark_suggestion_update_notified_sync,update_id)
                continue
            author=guild.get_member(author_id) or await guild.fetch_member(author_id)
            link=f'https://discord.com/channels/{guild_id}/{channel_id}/{message_id}'
            label=SUGGESTION_STATES[status][1]
            embed=discord.Embed(title='💡 تحديث اقتراحك',description=f"حالة اقتراحك في **{guild.name}** أصبحت: **{label}**\n\n{content}\n\n[فتح الاقتراح]({link})",color=discord.Color.from_rgb(*SUGGESTION_STATES[status][2]))
            embed.add_field(name='التحديث',value=f'<t:{int(created_at)}:f>',inline=False)
            await send_optional_member_dm(author,guild_id,'suggestions',embed=embed,allowed_mentions=discord.AllowedMentions.none())
        except (discord.NotFound,discord.Forbidden):
            pass
        except discord.HTTPException as exc:
            print(f'[Suggestions] تعذر إرسال تحديث لصاحب الاقتراح {author_id}: {exc}')
            continue
        await asyncio.to_thread(mark_suggestion_update_notified_sync,update_id)

@suggestion_workflow_task.before_loop
async def before_suggestion_workflow_task():
    await bot.wait_until_ready()


def build_suggestion_embed(author: discord.abc.User, guild: discord.Guild, content: str,
                            status: str = "pending", reviewer: discord.abc.User = None) -> discord.Embed:
    title,label,rgb=SUGGESTION_STATES.get(status,SUGGESTION_STATES['pending'])
    color=discord.Color.from_rgb(*rgb)
    embed = discord.Embed(title=title, description=content, color=color)
    embed.set_image(url=BANNER_IMAGE_URL)
    apply_branding(embed, guild, user=author)
    embed.add_field(name="👍 مع", value="0", inline=True)
    embed.add_field(name="👎 ضد", value="0", inline=True)
    embed.add_field(name="الحالة",value=label,inline=True)
    embed.add_field(name="صاحب الاقتراح",value=author.mention,inline=True)
    if status != "pending" and reviewer:
        embed.add_field(name="👤 راجعه", value=reviewer.mention, inline=True)
    return embed

class SuggestionModal(discord.ui.Modal, title="إرسال اقتراح جديد"):
    suggestion_input = discord.ui.TextInput(
        label="اكتب اقتراحك بالتفصيل",
        style=discord.TextStyle.paragraph,
        placeholder="مثال: إضافة روم صوتي مخصص للألعاب الجماعية...",
        max_length=1500
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        if not SUGGESTIONS_CHANNEL_ID:
            await interaction.followup.send("❌ لازم تحدد SUGGESTIONS_CHANNEL_ID بالكود الأول عشان تشتغل هذي الميزة.", ephemeral=True)
            return
        channel = guild.get_channel(SUGGESTIONS_CHANNEL_ID)
        if not channel:
            await interaction.followup.send("❌ ما قدرت ألقى روم الاقتراحات، تأكد من صحة SUGGESTIONS_CHANNEL_ID.", ephemeral=True)
            return

        embed = build_suggestion_embed(interaction.user, guild, self.suggestion_input.value)
        try:
            sent_msg = await channel.send(embed=embed, view=SuggestionVoteView(),allowed_mentions=discord.AllowedMentions.none())
            try:
                await asyncio.to_thread(save_suggestion_sync,sent_msg.id,guild.id,channel.id,interaction.user.id,self.suggestion_input.value)
            except Exception:
                await sent_msg.delete()
                raise
        except discord.Forbidden:
            await interaction.followup.send("❌ ما يملك البوت صلاحية الإرسال بروم الاقتراحات.", ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"❌ حدث خطأ أثناء نشر الاقتراح: {e}", ephemeral=True)
            return

        suggestion_votes[sent_msg.id] = {"up": set(), "down": set()}
        try:
            await sent_msg.add_reaction("👍")
        except Exception:
            pass
        await interaction.followup.send(f"✅ تم إرسال اقتراحك بنجاح: {sent_msg.jump_url}", ephemeral=True)

class SuggestionVoteView(ButtonLimitedView):
    def __init__(self,status='pending'):
        super().__init__(timeout=None)
        for child in self.children:
            if child.custom_id in ('suggestion_upvote','suggestion_downvote'):
                child.disabled=status not in ('pending','reviewing')
            else:
                target={'suggestion_review':'reviewing','suggestion_accept':'accepted','suggestion_reject':'rejected','suggestion_implemented':'implemented'}.get(child.custom_id)
                child.disabled=target not in SUGGESTION_TRANSITIONS.get(status,set())

    async def _update_votes(self,interaction,side):
        await interaction.response.defer(ephemeral=True)
        async with suggestion_action_lock:
            ok,error=await asyncio.to_thread(toggle_suggestion_vote_sync,interaction.message.id,interaction.guild.id,interaction.user.id,side)
            if not ok:
                await interaction.followup.send(error,ephemeral=True)
                return
            data=await asyncio.to_thread(suggestion_data_sync,interaction.message.id)
            try:
                await refresh_suggestion_message(interaction.message,data)
            except discord.HTTPException:
                await interaction.followup.send('صوتك محفوظ، وسيعاد تحديث الرسالة تلقائيًا.',ephemeral=True)
                return
            await interaction.followup.send('تم تحديث تصويتك.',ephemeral=True)

    @discord.ui.button(label='مع',emoji='👍',style=discord.ButtonStyle.success,custom_id='suggestion_upvote',row=0)
    async def upvote_btn(self,interaction,button):
        await self._update_votes(interaction,'up')

    @discord.ui.button(label='ضد',emoji='👎',style=discord.ButtonStyle.danger,custom_id='suggestion_downvote',row=0)
    async def downvote_btn(self,interaction,button):
        await self._update_votes(interaction,'down')

    async def _resolve(self,interaction,status):
        if not is_staff_member(interaction.user):
            await interaction.response.send_message('تغيير حالة الاقتراح متاح للإدارة فقط.',ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with suggestion_action_lock:
            ok,error=await asyncio.to_thread(change_suggestion_status_sync,interaction.message.id,interaction.guild.id,interaction.user.id,status)
            if not ok:
                await interaction.followup.send(error,ephemeral=True)
                return
            data=await asyncio.to_thread(suggestion_data_sync,interaction.message.id)
            try:
                await refresh_suggestion_message(interaction.message,data)
            except discord.HTTPException:
                await interaction.followup.send('الحالة محفوظة، وسيعاد تحديث رسالة الاقتراح تلقائيًا.',ephemeral=True)
                return
        await interaction.followup.send(f"تم تغيير الحالة إلى «{SUGGESTION_STATES[status][1]}». تحديث صاحب الاقتراح سيُرسل حسب تفضيلاته خلال دقيقة.",ephemeral=True)

    @discord.ui.button(label='قيد الدراسة',emoji='🔎',style=discord.ButtonStyle.secondary,custom_id='suggestion_review',row=1)
    async def review_btn(self,interaction,button):
        await self._resolve(interaction,'reviewing')

    @discord.ui.button(label='قبول',emoji='✅',style=discord.ButtonStyle.success,custom_id='suggestion_accept',row=1)
    async def accept_btn(self,interaction,button):
        await self._resolve(interaction,'accepted')

    @discord.ui.button(label='تم التنفيذ',emoji='🛠️',style=discord.ButtonStyle.primary,custom_id='suggestion_implemented',row=1)
    async def implemented_btn(self,interaction,button):
        await self._resolve(interaction,'implemented')

    @discord.ui.button(label='رفض',emoji='⛔',style=discord.ButtonStyle.danger,custom_id='suggestion_reject',row=1)
    async def reject_btn(self,interaction,button):
        await self._resolve(interaction,'rejected')

# ----------------- زر التصويت لمسابقة "أفضل لقطة أسبوعية" -----------------
class ClipVoteView(ButtonLimitedView):
    """View دائمة واحدة تُرسل تحت كل لقطة (صورة/فيديو) بروم اللقطات. الزر يعتمد على آيدي رسالة
    التصويت نفسها (interaction.message.id) لتحديد اللقطة المقصودة، فما نحتاج View منفصلة لكل لقطة."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="تصويت (0)", emoji="🗳️", style=discord.ButtonStyle.primary, custom_id="clip_vote_btn")
    async def vote_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild

        if not can_vote_on_clips(interaction.user):
            await interaction.response.send_message("❌ التصويت مقصور على قائمة الأعضاء المعتمدين، ما عندك صلاحية التصويت.", ephemeral=True)
            return

        vote_message_id = interaction.message.id

        author_id = await get_clip_entry_author(guild.id, vote_message_id)
        if author_id is None:
            await interaction.response.send_message("❌ ما قدرت ألقى بيانات هذي اللقطة (ممكن تكون قديمة من مسابقة سابقة).", ephemeral=True)
            return

        if interaction.user.id == author_id:
            await interaction.response.send_message("❌ ما تقدر تصوّت على لقطتك انت.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        status = await toggle_clip_vote(guild.id, vote_message_id, interaction.user.id)
        if status != 'added':
            errors = {
                'already': 'صوتك مسجل لهذه اللقطة بالفعل، ولا يمكن سحبه أو تغييره.',
                'used': 'استخدمت صوتك لهذا الأسبوع على لقطة أخرى. يتجدد بعد حسم مسابقة الأسبوع.',
                'self': 'ما تقدر تصوّت على لقطتك أنت.',
                'expired': 'انتهت الجولة أو لم تعد هذه اللقطة مشاركة فيها.',
                'forbidden': 'التصويت مقصور على قائمة الأعضاء المعتمدين.',
            }
            await interaction.followup.send('❌ ' + errors[status], ephemeral=True)
            return
        new_count = await get_clip_vote_count(guild.id, vote_message_id)

        updated_view = ClipVoteView()
        updated_view.children[0].label = f"تصويت ({new_count})"
        try:
            await interaction.message.edit(view=updated_view)
        except discord.HTTPException:
            pass
        await interaction.followup.send('✅ تم تسجيل صوتك النهائي لهذا الأسبوع.', ephemeral=True)
        await bump_quest(guild, interaction.user, "weekly_vote", 1)

        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(
                title="🗳️ تصويت على لقطة أسبوعية",
                color=discord.Color.blurple()
            )
            log_embed.add_field(name="👤 المصوّت", value=interaction.user.mention, inline=True)
            log_embed.add_field(name="🎬 صاحب اللقطة", value=f"<@{author_id}>", inline=True)
            log_embed.add_field(name="📊 إجمالي التصويتات الحالي", value=str(new_count), inline=True)
            log_embed.add_field(name="🔗 رابط اللقطة", value=f"[اضغط هنا]({interaction.message.jump_url})", inline=False)
            apply_log_branding(log_embed, guild, target=interaction.user)
            await log_channel.send(embed=log_embed)

    @discord.ui.button(label="مين صوّت؟", emoji="👥", style=discord.ButtonStyle.primary, custom_id="clip_voters_btn")
    async def voters_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if await get_clip_entry_author(interaction.guild.id, interaction.message.id) is None:
            await interaction.followup.send('انتهت مسابقة هذه اللقطة أو لم تعد مسجلة.', ephemeral=True)
            return
        voters = await asyncio.to_thread(get_clip_voters_sync, interaction.guild.id, interaction.message.id)
        if not voters:
            await interaction.followup.send('ما أحد صوّت لهذه اللقطة حتى الآن.', ephemeral=True)
            return
        text = '\n'.join(f'• <@{user_id}> (`{user_id}`)' for user_id in voters)
        if len(text) <= 3800:
            embed = discord.Embed(title=f'المصوّتون على اللقطة ({len(voters)})', description=text, color=discord.Color.blue())
            await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.followup.send('قائمة المصوّتين:', ephemeral=True,
                file=discord.File(io.BytesIO(text.encode('utf-8-sig')), filename='clip_voters.txt'))

# ----------------- نظام المهام اليومية والأسبوعية (Quests) -----------------
# روم إعلان إنجاز المهام (بالإضافة لروم السجلات)
QUEST_COMPLETE_ANNOUNCE_CHANNEL_ID = 1388414282739224727

# تعريف المهام اليومية: تتجدد تلقائياً كل يوم (بتوقيت الرياض) — المكافأة نقاط خبرة (XP) فقط، بدون ريالات
DAILY_QUEST_COUNT = 3
WEEKLY_QUEST_COUNT = 3

# كل عضو يحصل على أنواع مختلفة، ولكل نوع ثلاثة أهداف محتملة.
DAILY_QUESTS = {}
WEEKLY_QUESTS = {}
_QUEST_VARIANTS = {
    "daily": [
        ("chat", "daily_chat", "نشاط المجلس", "💬", "أرسل {} رسالة اليوم", (60, 90, 120), (10, 15, 20)),
        ("voice", "daily_voice", "حارس المجلس", "🎙️", "اقضِ {} دقيقة فويس نشط اليوم", (60, 90, 120), (15, 20, 25)),
        ("clip", "weekly_clip", "صانع اللقطات", "🎬", "شارك {} رسائل لقطات بروم اللقطات اليوم", (2, 3, 4), (10, 15, 20)),
        ("vote", "weekly_vote", "ناقد اللقطات", "🗳️", "صوّت على {} لقطات لأعضاء آخرين اليوم", (3, 5, 7), (10, 15, 20)),
    ],
    "weekly": [
        ("chat", "daily_chat", "ركن المجتمع", "💬", "أرسل {} رسالة هذا الأسبوع", (400, 600, 800), (300, 400, 500)),
        ("voice", "daily_voice", "فارس المجلس", "🎙️", "اقضِ {} دقيقة فويس نشط هذا الأسبوع", (360, 540, 720), (350, 450, 550)),
        ("checkin", "daily_checkin", "الحضور المستمر", "📅", "سجّل حضورك في {} أيام هذا الأسبوع", (4, 5, 6), (250, 350, 450)),
        ("xp", "weekly_xp", "سعي الفارس", "⭐", "اجمع {} XP من النشاط والحضور هذا الأسبوع", (2000, 3000, 4000), (350, 450, 550)),
        ("clip", "weekly_clip", "سجلّات البلاط", "🎬", "شارك {} رسائل لقطات هذا الأسبوع", (8, 12, 16), (300, 400, 500)),
        ("vote", "weekly_vote", "صوت الشعب", "🗳️", "صوّت على {} لقطات لأعضاء آخرين هذا الأسبوع", (15, 20, 25), (250, 350, 450)),
    ],
}
for _period, _variants in _QUEST_VARIANTS.items():
    _pool = DAILY_QUESTS if _period == "daily" else WEEKLY_QUESTS
    for _kind, _event, _title, _emoji, _desc, _targets, _rewards in _variants:
        for _target, _reward in zip(_targets, _rewards):
            _pool[f"{_period}_{_kind}_{_target}"] = {
                "title": _title, "desc": _desc.format(_target), "emoji": _emoji,
                "target": _target, "reward": _reward, "event": _event, "kind": _kind,
            }

# تنويع طريقة الإنجاز: أهداف يومية مركبة، وأهداف أسبوعية موزعة على أيام.
for _days,_reward in ((3,300),(4,400),(5,500)):
    for _kind,_title,_emoji,_requirements,_description in (
        ('chat_days','مجالس متواصلة','💬',{'daily_chat':40},'أرسل 40 رسالة في كل يوم من {} أيام مختلفة هذا الأسبوع'),
        ('voice_days','رفيق المجلس','🎙️',{'daily_voice':45},'اقضِ 45 دقيقة فويس نشط في كل يوم من {} أيام مختلفة هذا الأسبوع'),
        ('clip_days','لقطات متجددة','🎬',{'weekly_clip':1},'شارك لقطة في كل يوم من {} أيام مختلفة هذا الأسبوع'),
        ('xp_days','تقدم مستمر','⭐',{'weekly_xp':250},'اكسب 250 XP نشاط في كل يوم من {} أيام مختلفة هذا الأسبوع'),
        ('balanced_days','فارس متوازن','⚖️',{'daily_chat':20,'daily_voice':30},'أرسل 20 رسالة واقضِ 30 دقيقة فويس في كل يوم من {} أيام مختلفة هذا الأسبوع'),
    ):
        WEEKLY_QUESTS[f'weekly_{_kind}_{_days}']={
            'title':_title,'desc':_description.format(_days),'emoji':_emoji,
            'target':_days,'reward':_reward,'kind':_kind,'event':None,
            'mode':'days','requirements':_requirements,
        }
for _variant,_title,_description,_requirements,_reward in (
    ('balanced','جلسة متكاملة','أرسل 40 رسالة واقضِ 45 دقيقة فويس نشط اليوم',{'daily_chat':40,'daily_voice':45},25),
    ('clip_chat','شارك وناقش','شارك لقطتين وأرسل 40 رسالة اليوم',{'weekly_clip':2,'daily_chat':40},20),
    ('clip_voice','لقطات المجلس','شارك لقطتين واقضِ 45 دقيقة فويس نشط اليوم',{'weekly_clip':2,'daily_voice':45},25),
):
    DAILY_QUESTS[f'daily_mixed_{_variant}']={
        'title':_title,'desc':_description,'emoji':'⚖️','target':1,'reward':_reward,
        'kind':'mixed','event':None,'mode':'days','requirements':_requirements,
    }

# أهداف أسبوعية أصعب؛ تبقى معرّفات المهام ثابتة للحفاظ على التقدم المحفوظ.
_WEEKLY_HARD_TARGETS = {
    'chat': {400:1200,600:1800,800:2400},
    'voice': {360:1080,540:1620,720:2160},
    'checkin': {4:5,5:6,6:7},
    'xp': {2000:6000,3000:9000,4000:12000},
    'clip': {8:24,12:36,16:48},
    'vote': {15:45,20:60,25:75},
}
_WEEKLY_HARD_DAYS = {
    'chat_days': ({'daily_chat':120},'أرسل 120 رسالة في كل يوم من {} أيام مختلفة هذا الأسبوع'),
    'voice_days': ({'daily_voice':120},'اقضِ 120 دقيقة فويس نشط في كل يوم من {} أيام مختلفة هذا الأسبوع'),
    'clip_days': ({'weekly_clip':3},'شارك 3 لقطات في كل يوم من {} أيام مختلفة هذا الأسبوع'),
    'xp_days': ({'weekly_xp':1000},'اكسب 1000 XP نشاط في كل يوم من {} أيام مختلفة هذا الأسبوع'),
    'balanced_days': ({'daily_chat':80,'daily_voice':90},'أرسل 80 رسالة واقضِ 90 دقيقة فويس في كل يوم من {} أيام مختلفة هذا الأسبوع'),
}
for _qid,_qdef in WEEKLY_QUESTS.items():
    if _qdef.get('mode')=='days':
        _qdef['target']={3:5,4:6,5:7}[_qdef['target']]
        _qdef['requirements'],_template=_WEEKLY_HARD_DAYS[_qdef['kind']]
        _qdef['desc']=_template.format(_qdef['target'])
    else:
        _old_target=_qdef['target']
        _qdef['target']=_WEEKLY_HARD_TARGETS[_qdef['kind']][_old_target]
        _qdef['desc']=_qdef['desc'].replace(str(_old_target),str(_qdef['target']))

ALL_QUESTS = {}
for _qid, _qdef in DAILY_QUESTS.items():
    ALL_QUESTS[_qid] = {**_qdef, "period": "daily"}
for _qid, _qdef in WEEKLY_QUESTS.items():
    ALL_QUESTS[_qid] = {**_qdef, "period": "weekly"}

quest_db_lock = db_lock  # موحّد مع باقي عمليات القاعدة، راجع db_lock بالأعلى

cursor.execute("""
    CREATE TABLE IF NOT EXISTS quest_progress (
        guild_id INTEGER,
        user_id INTEGER,
        quest_id TEXT,
        progress INTEGER DEFAULT 0,
        claimed INTEGER DEFAULT 0,
        last_reset_date TEXT DEFAULT '',
        PRIMARY KEY (guild_id, user_id, quest_id)
    )
""")
conn.commit()

def get_quest_week_key() -> str:
    """يرجّع تاريخ آخر يوم جمعة (بتوقيت الرياض) كمفتاح للأسبوع الحالي — يتغيّر بالضبط
    لما يدخل أسبوع جديد، بنفس منطق تصفير لوحة المتصدرين الأسبوعية (الجمعة = weekday 4)."""
    now = datetime.datetime.now(RIYADH_TZ)
    days_since_friday = (now.weekday() - 4) % 7
    friday_date = (now - datetime.timedelta(days=days_since_friday)).date()
    return friday_date.isoformat()

cursor.execute("""
    CREATE TABLE IF NOT EXISTS quest_assignments (
        guild_id INTEGER, user_id INTEGER, period TEXT, period_key TEXT,
        quest_ids TEXT NOT NULL,
        PRIMARY KEY (guild_id, user_id, period)
    )
""")
conn.commit()

# استحقاقات المكافآت مستقلة عن دورة المهمة؛ تبقى حتى بعد تجدد المهام.
cursor.executescript("""
CREATE TABLE IF NOT EXISTS quest_day_activity (
    guild_id INTEGER,user_id INTEGER,quest_id TEXT,period_key TEXT,day_key TEXT,metric TEXT,amount INTEGER DEFAULT 0,
    PRIMARY KEY(guild_id,user_id,quest_id,period_key,day_key,metric)
);
CREATE TABLE IF NOT EXISTS quest_reward_payouts (
    guild_id INTEGER,user_id INTEGER,quest_id TEXT,period_key TEXT,
    reward INTEGER,title TEXT,description TEXT,emoji TEXT,period TEXT,
    paid INTEGER DEFAULT 0,notified INTEGER DEFAULT 0,
    new_level INTEGER DEFAULT 0,levels_gained INTEGER DEFAULT 0,level_reward INTEGER DEFAULT 0,
    PRIMARY KEY(guild_id,user_id,quest_id,period_key)
);
""")
conn.commit()

def quest_period_key(qdef):
    return (datetime.datetime.now(RIYADH_TZ).date().isoformat()
            if qdef['period']=='daily' else get_quest_week_key())

def quest_available_this_period(qdef, can_vote):
    if 'weekly_vote' in qdef.get('requirements', {}) or qdef.get('kind')=='vote':
        return False  # صوت واحد بالأسبوع لا يسمح بمهام تطلب عدة تصويتات.
    if qdef.get('mode')=='days' and qdef['period']=='weekly':
        now=datetime.datetime.now(RIYADH_TZ).date()
        end=datetime.date.fromisoformat(get_quest_week_key())+datetime.timedelta(days=6)
        return qdef['target'] <= (end-now).days+1
    return True

def _ensure_quest_fresh_locked(guild_id,user_id,quest_id,key):
    cursor.execute("SELECT last_reset_date FROM quest_progress WHERE guild_id=? AND user_id=? AND quest_id=?",(guild_id,user_id,quest_id))
    row=cursor.fetchone()
    if not row or row[0]!=key:
        cursor.execute("""INSERT INTO quest_progress VALUES (?, ?, ?, 0, 0, ?)
            ON CONFLICT(guild_id,user_id,quest_id) DO UPDATE SET progress=0,claimed=0,last_reset_date=excluded.last_reset_date""",(guild_id,user_id,quest_id,key))

def pay_quest_reward_sync(guild_id,user_id,quest_id,key):
    with db_lock:
        try:
            cursor.execute("SELECT reward,title,description,emoji,period,paid,new_level,levels_gained,level_reward FROM quest_reward_payouts WHERE guild_id=? AND user_id=? AND quest_id=? AND period_key=?",(guild_id,user_id,quest_id,key))
            row=cursor.fetchone()
            if not row:
                return None
            reward,title,description,emoji,period,paid,new_level,levels_gained,level_reward=row
            if not paid:
                new_level,leveled_up,_,levels_gained,_=_add_user_xp_locked(guild_id,user_id,reward,False)
                level_reward=levels_gained*LEVEL_UP_REWARD
                if level_reward>0:
                    cursor.execute("SELECT balance FROM wallets WHERE guild_id=? AND user_id=?",(guild_id,user_id))
                    wallet=cursor.fetchone()
                    balance=(wallet[0] if wallet else 0)+level_reward
                    cursor.execute("INSERT INTO wallets VALUES (?, ?, ?) ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=excluded.balance",(guild_id,user_id,balance))
                    record_bank_transaction_locked(guild_id,user_id,level_reward,balance,'مكافأة ترقية المستوى من مهمة')
                cursor.execute("UPDATE quest_reward_payouts SET paid=1,new_level=?,levels_gained=?,level_reward=? WHERE guild_id=? AND user_id=? AND quest_id=? AND period_key=?",(new_level,levels_gained,level_reward,guild_id,user_id,quest_id,key))
                conn.commit()
            return {"reward":reward,"title":title,"desc":description,"emoji":emoji,"period":period},new_level,levels_gained,level_reward
        except Exception:
            conn.rollback()
            raise

def pending_quest_payouts_sync(guild_id=None,user_id=None):
    with db_lock:
        sql="SELECT guild_id,user_id,quest_id,period_key FROM quest_reward_payouts WHERE paid=0 OR notified=0"
        args=[]
        if guild_id is not None:
            sql="SELECT guild_id,user_id,quest_id,period_key FROM quest_reward_payouts WHERE (paid=0 OR notified=0) AND guild_id=? AND user_id=?"
            args=[guild_id,user_id]
        cursor.execute(sql,args)
        return cursor.fetchall()

def mark_quest_notified_sync(guild_id,user_id,quest_id,key):
    with db_lock:
        cursor.execute("UPDATE quest_reward_payouts SET notified=1 WHERE guild_id=? AND user_id=? AND quest_id=? AND period_key=?",(guild_id,user_id,quest_id,key))
        conn.commit()

quest_payout_delivery_lock=asyncio.Lock()

async def settle_quest_rewards(guild_id=None,user_id=None):
    async with quest_payout_delivery_lock:
        pending=await asyncio.to_thread(pending_quest_payouts_sync,guild_id,user_id)
        for gid,uid,qid,key in pending:
            try:
                payout=await asyncio.to_thread(pay_quest_reward_sync,gid,uid,qid,key)
                if payout is None:
                    continue
                guild=bot.get_guild(gid)
                if not guild:
                    continue
                member=guild.get_member(uid)
                if not member:
                    try:
                        member=await guild.fetch_member(uid)
                    except discord.NotFound:
                        await asyncio.to_thread(mark_quest_notified_sync,gid,uid,qid,key)
                        continue
                qdef,new_level,levels_gained,level_reward=payout
                await announce_quest_reward(guild,member,qdef,new_level,levels_gained,level_reward)
                await asyncio.to_thread(mark_quest_notified_sync,gid,uid,qid,key)
            except Exception as exc:
                print(f"[Quests] استحقاق محفوظ وسيعاد صرفه/إشعاره: {gid}/{uid}/{qid}: {exc}")

@tasks.loop(minutes=1)
async def quest_reward_recovery_task():
    await settle_quest_rewards()

@quest_reward_recovery_task.before_loop
async def before_quest_reward_recovery_task():
    await bot.wait_until_ready()

def get_assigned_quests_sync(guild_id: int, user_id: int, can_vote: bool):
    assigned = []
    with quest_db_lock:
        for period, pool, count in (
            ("daily", DAILY_QUESTS, DAILY_QUEST_COUNT),
            ("weekly", WEEKLY_QUESTS, WEEKLY_QUEST_COUNT),
        ):
            key = (datetime.datetime.now(RIYADH_TZ).date().isoformat()
                   if period == "daily" else get_quest_week_key())
            cursor.execute("SELECT period_key, quest_ids FROM quest_assignments WHERE guild_id=? AND user_id=? AND period=?",
                           (guild_id, user_id, period))
            row = cursor.fetchone()
            ids = row[1].split(",") if row and row[0] == key else []
            if not ids or any(qid not in pool or pool[qid].get('kind') == 'vote'
                              or 'weekly_vote' in pool[qid].get('requirements', {}) for qid in ids):
                ids = [qid for qid in ids if qid in pool and pool[qid].get('kind') != 'vote'
                       and 'weekly_vote' not in pool[qid].get('requirements', {})]
                groups = {}
                for qid, qdef in pool.items():
                    if not quest_available_this_period({**qdef,"period":period},can_vote):
                        continue
                    groups.setdefault(qdef["kind"], []).append(qid)
                used_kinds = {pool[qid]['kind'] for qid in ids}
                remaining_kinds = [kind for kind in groups if kind not in used_kinds]
                kinds = random.sample(remaining_kinds, min(max(0, count - len(ids)), len(remaining_kinds)))
                ids.extend(random.choice(groups[kind]) for kind in kinds)
                cursor.execute("INSERT OR REPLACE INTO quest_assignments VALUES (?, ?, ?, ?, ?)",
                               (guild_id, user_id, period, key, ",".join(ids)))
                conn.commit()
            assigned.extend(ids)
    return assigned

async def bump_quest(guild: discord.Guild, member: discord.Member, quest_id: str, amount: int = 1):
    if guild is None or member is None or member.bot or amount <= 0:
        return
    await asyncio.to_thread(record_personal_activity_sync, guild.id, member.id, quest_id, amount)
    ids = await asyncio.to_thread(get_assigned_quests_sync, guild.id, member.id, can_vote_on_clips(member))
    for qid in ids:
        if ALL_QUESTS[qid]["event"] == quest_id or quest_id in ALL_QUESTS[qid].get("requirements",{}):
            await _complete_assigned_quest(guild, member, qid, amount, event=quest_id)

def _get_quest_row_sync(guild_id: int, user_id: int, quest_id: str):
    with quest_db_lock:
        cursor.execute(
            "SELECT progress, claimed FROM quest_progress WHERE guild_id = ? AND user_id = ? AND quest_id = ?",
            (guild_id, user_id, quest_id)
        )
        row = cursor.fetchone()
        return row if row else (0, 0)

def _ensure_quest_fresh_sync(guild_id: int, user_id: int, quest_id: str):
    """يتأكد إن صف المهمة محدّث حسب يومها/أسبوعها الحالي، ويصفّرها تلقائياً أول ما يبدأ
    يوم جديد (للمهام اليومية) أو أسبوع جديد (للمهام الأسبوعية) — بدون أي مهمة مجدولة منفصلة."""
    qdef = ALL_QUESTS[quest_id]
    period_key = (
        datetime.datetime.now(RIYADH_TZ).date().isoformat()
        if qdef["period"] == "daily" else get_quest_week_key()
    )
    with quest_db_lock:
        cursor.execute(
            "SELECT last_reset_date FROM quest_progress WHERE guild_id = ? AND user_id = ? AND quest_id = ?",
            (guild_id, user_id, quest_id)
        )
        row = cursor.fetchone()
        if not row or row[0] != period_key:
            cursor.execute("""
                INSERT INTO quest_progress (guild_id, user_id, quest_id, progress, claimed, last_reset_date)
                VALUES (?, ?, ?, 0, 0, ?)
                ON CONFLICT(guild_id, user_id, quest_id) DO UPDATE SET progress = 0, claimed = 0, last_reset_date = ?
            """, (guild_id, user_id, quest_id, period_key, period_key))
            conn.commit()

def increment_quest_progress_sync(guild_id: int, user_id: int, quest_id: str, amount: int, event=None):
    qdef=ALL_QUESTS[quest_id]
    key=quest_period_key(qdef)
    with quest_db_lock:
        try:
            cursor.execute("SELECT period_key,quest_ids FROM quest_assignments WHERE guild_id=? AND user_id=? AND period=?",(guild_id,user_id,qdef['period']))
            assigned=cursor.fetchone()
            if not assigned or assigned[0]!=key or quest_id not in assigned[1].split(','):
                return qdef,0,False
            _ensure_quest_fresh_locked(guild_id,user_id,quest_id,key)
            cursor.execute("SELECT progress,claimed FROM quest_progress WHERE guild_id=? AND user_id=? AND quest_id=?",(guild_id,user_id,quest_id))
            progress,claimed=cursor.fetchone()
            if claimed:
                return qdef,progress,False
            requirements=qdef.get('requirements')
            if requirements:
                if event not in requirements or amount<=0:
                    conn.commit()
                    return qdef,progress,False
                day=datetime.datetime.now(RIYADH_TZ).date().isoformat()
                cursor.execute("""INSERT INTO quest_day_activity VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id,user_id,quest_id,period_key,day_key,metric) DO UPDATE SET amount=amount+excluded.amount""",(guild_id,user_id,quest_id,key,day,event,amount))
                cursor.execute("SELECT day_key,metric,amount FROM quest_day_activity WHERE guild_id=? AND user_id=? AND quest_id=? AND period_key=?",(guild_id,user_id,quest_id,key))
                days={}
                for day,metric,value in cursor.fetchall():
                    days.setdefault(day,{})[metric]=value
                progress=sum(all(values.get(metric,0)>=minimum for metric,minimum in requirements.items()) for values in days.values())
            else:
                progress+=max(0,amount)
            progress=min(qdef['target'],progress)
            completed=progress>=qdef['target']
            cursor.execute("UPDATE quest_progress SET progress=?,claimed=? WHERE guild_id=? AND user_id=? AND quest_id=?",(progress,int(completed),guild_id,user_id,quest_id))
            if completed:
                cursor.execute("INSERT OR IGNORE INTO quest_reward_payouts(guild_id,user_id,quest_id,period_key,reward,title,description,emoji,period) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",(guild_id,user_id,quest_id,key,qdef['reward'],qdef['title'],qdef['desc'],qdef['emoji'],qdef['period']))
                cursor.execute("INSERT OR IGNORE INTO personal_quest_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",(guild_id,user_id,get_quest_week_key(),quest_id,key,qdef['title'],qdef['desc'],qdef['reward']))
            conn.commit()
            return qdef,progress,completed
        except Exception:
            conn.rollback()
            raise

async def increment_quest_progress(guild_id: int,user_id: int,quest_id: str,amount: int=1,event=None):
    return await asyncio.to_thread(increment_quest_progress_sync,guild_id,user_id,quest_id,amount,event)

async def _complete_assigned_quest(guild: discord.Guild,member: discord.Member,quest_id: str,amount: int=1,event=None):
    if member is None or member.bot or guild is None:
        return
    qdef,progress,completed=await increment_quest_progress(guild.id,member.id,quest_id,amount,event)
    if completed:
        await settle_quest_rewards(guild.id,member.id)

async def announce_quest_reward(guild,member,qdef,new_level,levels_gained,level_reward):
    if levels_gained:
        level_channel=bot.get_channel(LEVEL_CHANNEL_ID)
        if level_channel:
            level_embed=create_levelup_embed(member,new_level,guild,reward=level_reward)
            await level_channel.send(embed=level_embed)
        # الاسترداد قد يحصل بعد نشاط إضافي؛ لا نعيد رتبة أقدم للعضو.
        _,current_level,_=await get_user_data(guild.id,member.id)
        await update_member_level_role(member,current_level)

    period_label = "يومية" if qdef["period"] == "daily" else "أسبوعية"

    embed = discord.Embed(
        title=f"{qdef['emoji']} أنجزت مهمة {period_label}: {qdef['title']}",
        description=(
            f"{member.mention} أنجز **{qdef['desc']}**!\n\n"
            f"🎁 المكافأة: `{qdef['reward']:,} XP`"
        ),
        color=discord.Color.from_rgb(46, 204, 113)
    )
    apply_branding(embed, guild, user=member)
    announce_channel = bot.get_channel(QUEST_COMPLETE_ANNOUNCE_CHANNEL_ID)
    if announce_channel:
        try:
            await announce_channel.send(embed=embed)
        except Exception as e:
            print(f"[Quests] تعذّر إرسال إعلان إنجاز المهمة: {e}")

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        log_embed = discord.Embed(title="🧭 إنجاز مهمة", color=discord.Color.from_rgb(46, 204, 113))
        log_embed.add_field(name="👤 العضو", value=member.mention, inline=True)
        log_embed.add_field(name="📋 المهمة", value=f"{qdef['emoji']} {qdef['title']} ({period_label})", inline=True)
        log_embed.add_field(name="🎁 المكافأة", value=f"`{qdef['reward']:,} XP`", inline=True)
        apply_log_branding(log_embed, guild, target=member)
        try:
            await log_channel.send(embed=log_embed)
        except Exception as e:
            print(f"[Quests] تعذّر إرسال سجل إنجاز المهمة: {e}")

def build_quest_progress_bar(progress: int, target: int, length: int = 10) -> str:
    if target <= 0:
        return "▰" * length
    filled = int(length * min(1.0, progress / target))
    return "▰" * filled + "▱" * (length - filled)

def quest_display_details_sync(guild_id,user_id,quest_id):
    qdef=ALL_QUESTS[quest_id]
    key=quest_period_key(qdef)
    day=datetime.datetime.now(RIYADH_TZ).date().isoformat()
    with db_lock:
        cursor.execute("SELECT metric,amount FROM quest_day_activity WHERE guild_id=? AND user_id=? AND quest_id=? AND period_key=? AND day_key=?",(guild_id,user_id,quest_id,key,day))
        today=dict(cursor.fetchall())
        cursor.execute("SELECT paid FROM quest_reward_payouts WHERE guild_id=? AND user_id=? AND quest_id=? AND period_key=?",(guild_id,user_id,quest_id,key))
        payout=cursor.fetchone()
    labels={'daily_chat':'الرسائل','daily_voice':'دقائق الفويس','weekly_clip':'اللقطات','weekly_xp':'XP النشاط'}
    details=' • '.join(f"{labels.get(metric,metric)} {min(today.get(metric,0),target)}/{target}" for metric,target in qdef.get('requirements',{}).items())
    return details,bool(payout and not payout[0])

async def build_quests_embed(guild: discord.Guild, member: discord.Member) -> discord.Embed:
    lines_daily, lines_weekly = [], []
    ids = await asyncio.to_thread(get_assigned_quests_sync, guild.id, member.id, can_vote_on_clips(member))
    for quest_id in ids:
        qdef = ALL_QUESTS[quest_id]
        await asyncio.to_thread(_ensure_quest_fresh_sync, guild.id, member.id, quest_id)
        progress, claimed = await asyncio.to_thread(_get_quest_row_sync, guild.id, member.id, quest_id)
        details,pending=await asyncio.to_thread(quest_display_details_sync,guild.id,member.id,quest_id)
        bar = build_quest_progress_bar(progress, qdef["target"])
        status = "✅ اكتملت" if claimed else f"`{min(progress, qdef['target'])}/{qdef['target']}`"
        if pending:
            status="⏳ اكتملت — المكافأة محفوظة وتنتظر الصرف"
        line = f"{qdef['emoji']} **{qdef['title']}** — {qdef['desc']}\n{bar}  {status}"
        if details and not claimed:
            line+=f"\nتقدم اليوم: {details}"
        (lines_daily if qdef["period"] == "daily" else lines_weekly).append(line)

    embed = discord.Embed(title="🧭 مهامي اليومية والأسبوعية", description="لك 3 مهام يومية و3 أسبوعية عشوائية تثبت حتى التجديد. قد تتشابه بعض المهام بين الأعضاء.", color=discord.Color.from_rgb(0, 168, 252))
    for title,lines in (("☀️ مهام يومية (تتجدد كل يوم)",lines_daily),("🗓️ مهام أسبوعية (تتجدد كل جمعة)",lines_weekly)):
        chunk=""
        for line in lines:
            if chunk and len(chunk)+len(line)+2>1000:
                embed.add_field(name=title,value=chunk,inline=False)
                chunk=""
            chunk+=("\n\n" if chunk else "")+line
        if chunk:
            embed.add_field(name=title,value=chunk,inline=False)
    apply_branding(embed, guild, user=member)
    return embed

# ----------------- التقرير الشخصي وتبديل المهام -----------------
QUEST_REROLL_COST = 25
REPORT_METRICS = {
    "daily_chat": "💬 الرسائل", "daily_voice": "🎙️ دقائق الفويس النشط",
    "daily_checkin": "📅 أيام الحضور", "weekly_xp": "⭐ خبرة النشاط",
    "weekly_clip": "🎬 مشاركات اللقطات", "weekly_vote": "🗳️ التصويتات",
}
cursor.executescript("""
CREATE TABLE IF NOT EXISTS personal_weekly_activity (
    guild_id INTEGER, user_id INTEGER, week_key TEXT, metric TEXT, amount INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, week_key, metric)
);
CREATE TABLE IF NOT EXISTS personal_quest_history (
    guild_id INTEGER, user_id INTEGER, week_key TEXT, quest_id TEXT, period_key TEXT,
    title TEXT, description TEXT, reward INTEGER,
    PRIMARY KEY (guild_id, user_id, quest_id, period_key)
);
CREATE TABLE IF NOT EXISTS quest_rerolls (
    guild_id INTEGER, user_id INTEGER, day_key TEXT, old_id TEXT, new_id TEXT, cost INTEGER,
    PRIMARY KEY (guild_id, user_id, day_key)
);
CREATE TABLE IF NOT EXISTS personal_report_meta (key TEXT PRIMARY KEY, value TEXT);
""")
cursor.execute("INSERT OR IGNORE INTO personal_report_meta VALUES ('tracking_started', ?)",
               (datetime.datetime.now(RIYADH_TZ).date().isoformat(),))
conn.commit()

def record_personal_activity_sync(guild_id, user_id, metric, amount):
    if metric not in REPORT_METRICS or amount <= 0:
        return
    with db_lock:
        cursor.execute("""INSERT INTO personal_weekly_activity VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id,user_id,week_key,metric) DO UPDATE SET amount=amount+excluded.amount""",
            (guild_id, user_id, get_quest_week_key(), metric, amount))
        conn.commit()

def personal_report_data_sync(guild_id, user_id, week_key):
    previous = (datetime.date.fromisoformat(week_key) - datetime.timedelta(days=7)).isoformat()
    with db_lock:
        cursor.execute("SELECT week_key,metric,amount FROM personal_weekly_activity WHERE guild_id=? AND user_id=? AND week_key IN (?,?)",
                       (guild_id, user_id, week_key, previous))
        values = {(week, metric): amount for week, metric, amount in cursor.fetchall()}
        cursor.execute("SELECT title,description,reward FROM personal_quest_history WHERE guild_id=? AND user_id=? AND week_key=? ORDER BY period_key,quest_id",
                       (guild_id, user_id, week_key))
        completed = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) FROM personal_quest_history WHERE guild_id=? AND user_id=? AND week_key=?",
                       (guild_id, user_id, previous))
        previous_completed = cursor.fetchone()[0]
        cursor.execute("SELECT value FROM personal_report_meta WHERE key='tracking_started'")
        started = cursor.fetchone()[0]
    return previous, values, completed, previous_completed, started

async def build_personal_report_embed(guild, member, week_key=None):
    week_key = week_key or get_quest_week_key()
    previous, values, completed, previous_completed, started = await asyncio.to_thread(
        personal_report_data_sync, guild.id, member.id, week_key)
    previous_complete = started <= previous
    end = (datetime.date.fromisoformat(week_key) + datetime.timedelta(days=6)).isoformat()
    embed = discord.Embed(title="📊 تقريرك الشخصي الأسبوعي", description=f"الفترة: {week_key} إلى {end} (الرياض)",
                          color=discord.Color.from_rgb(0,168,252))
    lines = []
    for metric, label in REPORT_METRICS.items():
        current = values.get((week_key, metric), 0)
        before = values.get((previous, metric), 0)
        comparison = f"{current-before:+,} عن الأسبوع السابق ({before:,})" if previous_complete else "المقارنة غير متاحة: سجل الأسبوع السابق غير مكتمل"
        lines.append(f"{label}: **{current:,}** — {comparison}")
    embed.add_field(name="نشاطك", value="\n".join(lines), inline=False)
    delta = f" ({len(completed)-previous_completed:+} عن الأسبوع السابق)" if previous_complete else ""
    embed.add_field(name="المهام المكتملة", value=f"**{len(completed)} مهمة**{delta}\nمكافآت المهام: **{sum(row[2] for row in completed):,} XP**", inline=False)
    # تجميع الإنجازات المتكررة يحافظ على حدود الإيمبيد.
    groups = {}
    for title, desc, reward in completed:
        groups[desc] = groups.get(desc, 0) + 1
    details = [f"✅ {desc}" + (f" ×{count}" if count > 1 else "") for desc, count in groups.items()]
    chunks, chunk = [], ""
    for line in details:
        if len(chunk) + len(line) + 1 > 950:
            chunks.append(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        chunks.append(chunk)
    for index, chunk in enumerate(chunks):
        embed.add_field(name=f"إنجازاتك ({index+1})", value=chunk, inline=False)
    if not completed:
        embed.add_field(name="إنجازاتك", value="ما خلّصت مهام مسجلة في هالفترة بعد.", inline=False)
    embed.set_footer(text=f"بدأ التسجيل من {started}. الفويس يحسب الدقائق النشطة فقط؛ خبرة المهام منفصلة عن خبرة النشاط.")
    return embed

def reroll_quest_sync(guild_id, user_id, quest_id, can_vote):
    today = datetime.datetime.now(RIYADH_TZ).date().isoformat()
    if quest_id not in ALL_QUESTS:
        return False, "المهمة غير موجودة."
    period = ALL_QUESTS[quest_id]["period"]
    key = today if period == "daily" else get_quest_week_key()
    pool = DAILY_QUESTS if period == "daily" else WEEKLY_QUESTS
    with db_lock:
        try:
            cursor.execute("SELECT 1 FROM quest_rerolls WHERE guild_id=? AND user_id=? AND day_key=?", (guild_id,user_id,today))
            if cursor.fetchone():
                return False, "بدّلت مهمة اليوم بالفعل؛ تقدر تبدّل مرة ثانية بعد منتصف الليل بتوقيت الرياض."
            cursor.execute("SELECT period_key,quest_ids FROM quest_assignments WHERE guild_id=? AND user_id=? AND period=?", (guild_id,user_id,period))
            row = cursor.fetchone()
            if not row or row[0] != key or quest_id not in row[1].split(','):
                return False, "قائمة مهامك تغيّرت؛ افتح مهامي من جديد."
            ids = row[1].split(',')
            cursor.execute("SELECT claimed FROM quest_progress WHERE guild_id=? AND user_id=? AND quest_id=? AND last_reset_date=?", (guild_id,user_id,quest_id,key))
            progress_row = cursor.fetchone()
            if progress_row and progress_row[0]:
                return False, "ما تقدر تبدّل مهمة مكتملة."
            occupied = {ALL_QUESTS[qid]['kind'] for qid in ids if qid != quest_id}
            cursor.execute("SELECT quest_id FROM quest_progress WHERE guild_id=? AND user_id=? AND last_reset_date=?", (guild_id,user_id,key))
            used = {r[0] for r in cursor.fetchall()} | set(ids)
            # استبعاد أهداف من نفس النشاط حتى تجيه مهمة مختلفة بالفعل.
            candidates = [qid for qid,qdef in pool.items() if qid not in used
                          and qdef['kind'] not in occupied
                          and qdef['kind'] != ALL_QUESTS[quest_id]['kind']
                          and quest_available_this_period({**qdef,'period':period},can_vote)]
            if not candidates:
                candidates = [qid for qid,qdef in pool.items() if qid not in used
                              and qdef['kind'] == ALL_QUESTS[quest_id]['kind']
                              and quest_available_this_period({**qdef,'period':period},can_vote)]
            if not candidates:
                return False, "ما فيه نوع مهمة بديل متاح حاليًا؛ ما انخصم شيء."
            cursor.execute("SELECT balance FROM wallets WHERE guild_id=? AND user_id=?", (guild_id,user_id))
            wallet = cursor.fetchone()
            if not wallet or wallet[0] < QUEST_REROLL_COST:
                return False, f"رصيدك ما يكفي؛ التبديل يحتاج {QUEST_REROLL_COST} ريال."
            new_id = random.choice(candidates)
            ids[ids.index(quest_id)] = new_id
            cursor.execute("UPDATE wallets SET balance=balance-? WHERE guild_id=? AND user_id=?", (QUEST_REROLL_COST,guild_id,user_id))
            record_bank_transaction_locked(guild_id,user_id,-QUEST_REROLL_COST,wallet[0]-QUEST_REROLL_COST,"تبديل مهمة")
            cursor.execute("UPDATE quest_assignments SET quest_ids=? WHERE guild_id=? AND user_id=? AND period=?", (','.join(ids),guild_id,user_id,period))
            cursor.execute("INSERT OR REPLACE INTO quest_progress VALUES (?, ?, ?, 0, 0, ?)", (guild_id,user_id,new_id,key))
            cursor.execute("INSERT INTO quest_rerolls VALUES (?, ?, ?, ?, ?, ?)", (guild_id,user_id,today,quest_id,new_id,QUEST_REROLL_COST))
            conn.commit()
            return True, f"تم تبديل المهمة مقابل {QUEST_REROLL_COST} ريال. مهمتك الجديدة: {ALL_QUESTS[new_id]['desc']}. تبدأ من صفر."
        except Exception:
            conn.rollback()
            raise

class QuestRerollSelect(discord.ui.Select):
    def __init__(self, ids):
        super().__init__(placeholder=f"اختر مهمة لتبديلها مقابل {QUEST_REROLL_COST} ريال", options=[
            discord.SelectOption(label=ALL_QUESTS[qid]['desc'][:100], value=qid, emoji=ALL_QUESTS[qid]['emoji']) for qid in ids])

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        ok, message = await asyncio.to_thread(reroll_quest_sync, interaction.guild.id, interaction.user.id,
                                             self.values[0], can_vote_on_clips(interaction.user))
        await interaction.followup.send(message, ephemeral=True)
        if ok:
            await interaction.edit_original_response(
                content=message,
                embed=await build_quests_embed(interaction.guild, interaction.user),
                view=PersonalQuestView(interaction.user.id))

class QuestRerollView(ButtonLimitedView):
    def __init__(self, owner_id, ids):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.add_item(QuestRerollSelect(ids))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("هذي القائمة تخص صاحبها.", ephemeral=True)
            return False
        return True

class PersonalQuestView(ButtonLimitedView):
    def __init__(self, owner_id):
        super().__init__(timeout=180)
        self.owner_id = owner_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("هذي اللوحة تخص صاحبها.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="تقريري الأسبوعي", emoji="📊", style=discord.ButtonStyle.secondary)
    async def report(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=await build_personal_report_embed(interaction.guild, interaction.user), ephemeral=True)

    @discord.ui.button(label="تقرير الأسبوع السابق", emoji="🗓️", style=discord.ButtonStyle.secondary)
    async def previous_report(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        key = (datetime.date.fromisoformat(get_quest_week_key())-datetime.timedelta(days=7)).isoformat()
        await interaction.followup.send(embed=await build_personal_report_embed(interaction.guild, interaction.user, key), ephemeral=True)

    @discord.ui.button(label="تبديل مهمة · 25 ريال", emoji="🔄", style=discord.ButtonStyle.primary)
    async def reroll(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        ids = await asyncio.to_thread(get_assigned_quests_sync, interaction.guild.id, interaction.user.id, can_vote_on_clips(interaction.user))
        available = []
        for qid in ids:
            await asyncio.to_thread(_ensure_quest_fresh_sync, interaction.guild.id, interaction.user.id, qid)
            progress, claimed = await asyncio.to_thread(_get_quest_row_sync, interaction.guild.id, interaction.user.id, qid)
            if not claimed:
                available.append(qid)
        if not available:
            await interaction.followup.send("كل مهامك مكتملة؛ ما فيه مهمة للتبديل.", ephemeral=True)
            return
        await interaction.followup.send(f"تبديل واحد باليوم لليومية أو الأسبوعية، مقابل {QUEST_REROLL_COST} ريال. المهمة البديلة تبدأ من صفر. اختيارك من القائمة ينفذ التبديل والخصم.",
                                        view=QuestRerollView(interaction.user.id, available), ephemeral=True)

    @discord.ui.button(label="تفضيلاتي",emoji="⚙️",style=discord.ButtonStyle.secondary)
    async def preferences(self,interaction,button):
        await show_member_preferences(interaction)

@bot.tree.command(name="weekly_report", description="تقرير نشاطك ومهامك الأسبوعية ومقارنتها بالأسبوع السابق")
@app_commands.guild_only()
async def weekly_report_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(embed=await build_personal_report_embed(interaction.guild, interaction.user),
                                    view=PersonalQuestView(interaction.user.id), ephemeral=True)



cursor.execute("""CREATE TABLE IF NOT EXISTS personal_report_delivery (
    guild_id INTEGER, user_id INTEGER, week_key TEXT, status TEXT,
    PRIMARY KEY(guild_id,user_id,week_key))""")
conn.commit()

def pending_personal_reports_sync(week_key):
    with db_lock:
        cursor.execute("""SELECT DISTINCT a.guild_id,a.user_id FROM personal_weekly_activity a
            LEFT JOIN personal_report_delivery d ON d.guild_id=a.guild_id AND d.user_id=a.user_id AND d.week_key=a.week_key
            WHERE a.week_key=? AND d.status IS NULL""", (week_key,))
        return cursor.fetchall()

def mark_personal_report_sync(guild_id,user_id,key,status):
    with db_lock:
        cursor.execute("INSERT OR REPLACE INTO personal_report_delivery VALUES (?, ?, ?, ?)", (guild_id,user_id,key,status))
        conn.commit()

cursor.execute("""CREATE TABLE IF NOT EXISTS personal_report_expiry (
    channel_id INTEGER, message_id INTEGER PRIMARY KEY, delete_at REAL NOT NULL
)""")
conn.commit()

def save_personal_report_expiry_sync(channel_id, message_id, delete_at):
    with db_lock:
        cursor.execute("INSERT OR REPLACE INTO personal_report_expiry VALUES (?, ?, ?)",
                       (channel_id, message_id, delete_at))
        conn.commit()

def due_personal_report_expiry_sync():
    with db_lock:
        cursor.execute("SELECT channel_id,message_id FROM personal_report_expiry WHERE delete_at <= ?", (time.time(),))
        return cursor.fetchall()

def clear_personal_report_expiry_sync(message_id):
    with db_lock:
        cursor.execute("DELETE FROM personal_report_expiry WHERE message_id=?", (message_id,))
        conn.commit()

@tasks.loop(seconds=30)
async def personal_report_cleanup_task():
    for channel_id, message_id in await asyncio.to_thread(due_personal_report_expiry_sync):
        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            await channel.get_partial_message(message_id).delete()
        except discord.NotFound:
            pass  # انحذفت مسبقًا.
        except discord.HTTPException as exc:
            print(f"[Personal Report] تعذر حذف التقرير {message_id}: {exc}")
            continue  # إعادة المحاولة في الفحص التالي.
        await asyncio.to_thread(clear_personal_report_expiry_sync, message_id)

@personal_report_cleanup_task.before_loop
async def before_personal_report_cleanup_task():
    await bot.wait_until_ready()

@tasks.loop(minutes=10)
async def personal_weekly_report_task():
    # التقرير عن الأسبوع المنتهي، مع تعويض التشغيل المتأخر طوال الأسبوع التالي.
    key = (datetime.date.fromisoformat(get_quest_week_key())-datetime.timedelta(days=7)).isoformat()
    for guild_id,user_id in await asyncio.to_thread(pending_personal_reports_sync, key):
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue
        preferences = await asyncio.to_thread(member_preferences_sync,guild_id,user_id)
        if not preferences['weekly_report']:
            await asyncio.to_thread(mark_personal_report_sync,guild_id,user_id,key,'opted_out')
            continue
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                await asyncio.to_thread(mark_personal_report_sync,guild_id,user_id,key,'left')
                continue
            except discord.HTTPException:
                continue
        try:
            embed = await build_personal_report_embed(guild,member,key)
            embed.set_footer(text=f"{embed.footer.text or ''} • ينحذف هذا التقرير تلقائيًا بعد 24 ساعة.")
            report_message = await send_optional_member_dm(member,guild_id,'weekly_report',embed=embed)
            if report_message is None:
                await asyncio.to_thread(mark_personal_report_sync,guild_id,user_id,key,'opted_out')
                continue
            await asyncio.to_thread(save_personal_report_expiry_sync, report_message.channel.id,
                                    report_message.id, report_message.created_at.timestamp() + 24 * 3600)
        except discord.Forbidden:
            await asyncio.to_thread(mark_personal_report_sync,guild_id,user_id,key,'dm_closed')
        except discord.HTTPException as exc:
            print(f"[Personal Report] تعذر إرسال تقرير {user_id}: {exc}")
        else:
            await asyncio.to_thread(mark_personal_report_sync,guild_id,user_id,key,'sent')
        await asyncio.sleep(1)

@personal_weekly_report_task.before_loop
async def before_personal_weekly_report_task():
    await bot.wait_until_ready()

# ----------------- تفضيلات العضو وسجل معاملات البنك -----------------
MEMBER_DM_OPTIONS = {
    "suggestions": "تحديثات اقتراحاتي",
    "weekly_report": "التقرير الأسبوعي",
    "financial": "إشعارات التحويل والراتب",
    "activity": "تنبيه سقف خبرة الفويس",
    "welcome": "الترحيب بالخاص",
}
cursor.executescript("""
CREATE TABLE IF NOT EXISTS member_dm_preferences (
    guild_id INTEGER, user_id INTEGER, category TEXT, enabled INTEGER NOT NULL,
    PRIMARY KEY(guild_id,user_id,category)
);
CREATE TABLE IF NOT EXISTS bank_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL, amount INTEGER NOT NULL, balance_after INTEGER NOT NULL,
    reason TEXT NOT NULL, counterparty_id INTEGER, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS bank_transactions_member ON bank_transactions(guild_id,user_id,id);
""")
conn.commit()

def member_preferences_sync(guild_id, user_id):
    with db_lock:
        cursor.execute("SELECT category,enabled FROM member_dm_preferences WHERE guild_id=? AND user_id=?", (guild_id,user_id))
        saved = dict(cursor.fetchall())
    return {key: bool(saved.get(key, 1)) for key in MEMBER_DM_OPTIONS}

def toggle_member_preference_sync(guild_id,user_id,category):
    if category not in MEMBER_DM_OPTIONS:
        raise ValueError("Unknown preference")
    with db_lock:
        cursor.execute("SELECT enabled FROM member_dm_preferences WHERE guild_id=? AND user_id=? AND category=?", (guild_id,user_id,category))
        row = cursor.fetchone()
        enabled = 0 if row is None or row[0] else 1
        cursor.execute("INSERT OR REPLACE INTO member_dm_preferences VALUES (?, ?, ?, ?)", (guild_id,user_id,category,enabled))
        conn.commit()
    return bool(enabled)

async def send_optional_member_dm(member, guild_id, category, **kwargs):
    preferences = await asyncio.to_thread(member_preferences_sync, guild_id, member.id)
    if preferences[category]:
        return await member.send(**kwargs)
    return None

# القفل يُمسك في العملية المالية نفسها؛ لا نمسكه مرة ثانية هنا.
def record_bank_transaction_locked(guild_id,user_id,amount,balance_after,reason,counterparty_id=None):
    if amount:
        cursor.execute("INSERT INTO bank_transactions(guild_id,user_id,amount,balance_after,reason,counterparty_id,created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (guild_id,user_id,amount,balance_after,reason,counterparty_id,time.time()))

def transfer_bank_sync(guild_id,sender_id,recipient_id,amount):
    if amount <= 0 or sender_id == recipient_id:
        return None
    with db_lock:
        try:
            cursor.execute("SELECT balance FROM wallets WHERE guild_id=? AND user_id=?", (guild_id,sender_id))
            row = cursor.fetchone()
            if row is None or row[0] < amount:
                return None
            sender_balance = row[0]-amount
            cursor.execute("SELECT balance FROM wallets WHERE guild_id=? AND user_id=?", (guild_id,recipient_id))
            row = cursor.fetchone()
            recipient_balance = (row[0] if row else 0)+amount
            cursor.execute("UPDATE wallets SET balance=? WHERE guild_id=? AND user_id=?", (sender_balance,guild_id,sender_id))
            cursor.execute("INSERT INTO wallets VALUES (?, ?, ?) ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=excluded.balance", (guild_id,recipient_id,recipient_balance))
            record_bank_transaction_locked(guild_id,sender_id,-amount,sender_balance,"تحويل صادر",recipient_id)
            record_bank_transaction_locked(guild_id,recipient_id,amount,recipient_balance,"تحويل وارد",sender_id)
            conn.commit()
            return sender_balance
        except Exception:
            conn.rollback()
            raise

def bank_history_sync(guild_id,user_id,page):
    page = max(0,page)
    with db_lock:
        cursor.execute("SELECT COUNT(*) FROM bank_transactions WHERE guild_id=? AND user_id=?", (guild_id,user_id))
        total = cursor.fetchone()[0]
        page = min(page,max(0,(total-1)//8))
        cursor.execute("SELECT amount,balance_after,reason,counterparty_id,created_at FROM bank_transactions WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 8 OFFSET ?", (guild_id,user_id,page*8))
        rows = cursor.fetchall()
    return rows,total,page

async def build_bank_history_embed(guild_id,user_id,page=0):
    rows,total,page = await asyncio.to_thread(bank_history_sync,guild_id,user_id,page)
    embed = discord.Embed(title="📒 سجل معاملات البنك",color=discord.Color.gold())
    for amount,balance,reason,other,created in rows:
        party = f" • العضو: <@{other}>" if other else ""
        embed.add_field(name=f"{'➕' if amount>0 else '➖'} {amount:+,} {CURRENCY_NAME} — {reason[:150]}",
                        value=f"<t:{int(created)}:f>{party}\nالرصيد بعد العملية: **{balance:,} {CURRENCY_NAME}**",inline=False)
    if not rows:
        embed.description="ما عندك معاملات مسجلة بعد. يبدأ السجل من تشغيل النسخة الجديدة."
    embed.set_footer(text=f"صفحة {page+1}/{max(1,(total+7)//8)} • {total} عملية • العمليات السابقة لتركيب النظام غير مسجلة")
    return embed,page,total

class BankHistoryView(ButtonLimitedView):
    def __init__(self,owner_id,page=0,total=0):
        super().__init__(timeout=180)
        self.owner_id,self.page=owner_id,page
        self.previous.disabled=page<=0
        self.next.disabled=(page+1)*8>=total

    async def interaction_check(self,interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("هذا السجل يخص صاحبه.",ephemeral=True)
            return False
        return True

    async def show_page(self,interaction,page):
        await interaction.response.defer()
        embed,page,total=await build_bank_history_embed(interaction.guild.id,interaction.user.id,page)
        await interaction.edit_original_response(embed=embed,view=BankHistoryView(self.owner_id,page,total),allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="السابق",style=discord.ButtonStyle.secondary)
    async def previous(self,interaction,button):
        await self.show_page(interaction,self.page-1)

    @discord.ui.button(label="التالي",style=discord.ButtonStyle.secondary)
    async def next(self,interaction,button):
        await self.show_page(interaction,self.page+1)

async def show_bank_history(interaction):
    await interaction.response.defer(ephemeral=True)
    embed,page,total=await build_bank_history_embed(interaction.guild.id,interaction.user.id)
    await interaction.followup.send(embed=embed,view=BankHistoryView(interaction.user.id,page,total),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

async def build_member_preferences_embed(guild_id,user_id):
    preferences=await asyncio.to_thread(member_preferences_sync,guild_id,user_id)
    lines=[f"{'✅ مفعّل' if preferences[key] else '⏸️ متوقف'} — {label}" for key,label in MEMBER_DM_OPTIONS.items()]
    return discord.Embed(title="⚙️ تفضيلات رسائلك الخاصة",description="\n".join(lines)+"\n\nاضغط الخيار لتشغيله أو إيقافه. التقرير يظل متاحًا عند الطلب، وقرارات الإدارة مثل الإنذارات والاستدعاء تبقى مستقلة عن التفضيلات.",color=discord.Color.from_rgb(0,168,252))

class PreferenceToggleButton(discord.ui.Button):
    def __init__(self,category,label):
        super().__init__(label=label,style=discord.ButtonStyle.secondary)
        self.category=category

    async def callback(self,interaction):
        await interaction.response.defer()
        await asyncio.to_thread(toggle_member_preference_sync,interaction.guild.id,interaction.user.id,self.category)
        await interaction.edit_original_response(embed=await build_member_preferences_embed(interaction.guild.id,interaction.user.id))

class MemberPreferencesView(ButtonLimitedView):
    def __init__(self,owner_id):
        super().__init__(timeout=180)
        self.owner_id=owner_id
        for category,label in MEMBER_DM_OPTIONS.items():
            self.add_item(PreferenceToggleButton(category,label))

    async def interaction_check(self,interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("هذي التفضيلات تخص صاحبها.",ephemeral=True)
            return False
        return True

async def show_member_preferences(interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(embed=await build_member_preferences_embed(interaction.guild.id,interaction.user.id),view=MemberPreferencesView(interaction.user.id),ephemeral=True)

class MemberBankView(ButtonLimitedView):
    def __init__(self,owner_id):
        super().__init__(timeout=180)
        self.owner_id=owner_id

    async def interaction_check(self,interaction):
        if interaction.user.id!=self.owner_id:
            await interaction.response.send_message("هذه اللوحة تخص صاحبها.",ephemeral=True)
            return False
        return True

    @discord.ui.button(label="سجل المعاملات",emoji="📒",style=discord.ButtonStyle.secondary)
    async def history(self,interaction,button):
        await show_bank_history(interaction)

    @discord.ui.button(label="تفضيلاتي",emoji="⚙️",style=discord.ButtonStyle.secondary)
    async def preferences(self,interaction,button):
        await show_member_preferences(interaction)

@bot.tree.command(name="preferences",description="تشغيل أو إيقاف التقارير والتذكيرات الخاصة")
@app_commands.guild_only()
async def member_preferences_command(interaction: discord.Interaction):
    await show_member_preferences(interaction)

@bot.tree.command(name="bank_history",description="عرض آخر التحويلات والمكافآت والمشتريات وخصوماتك")
@app_commands.guild_only()
async def bank_history_command(interaction: discord.Interaction):
    await show_bank_history(interaction)


# ----------------- لوحة مساهمات الفريق -----------------
def war_personal_team_sync(guild_id: int, user_id: int):
    with db_lock:
        week_id = _war_active_week_locked(guild_id)
        if week_id is None:
            return None, "⚔️ ما فيه حرب فرق نشطة حاليًا."
        cursor.execute("SELECT team_idx FROM war_members WHERE guild_id=? AND week_id=? AND user_id=?",
                       (guild_id,week_id,user_id))
        membership = cursor.fetchone()
        if not membership:
            return None, "ما أنت ضمن فريق في الجولة الحالية."
        team_idx = membership[0]
        cursor.execute("SELECT name,emoji,leader_id FROM war_teams WHERE guild_id=? AND week_id=? AND team_idx=?",
                       (guild_id,week_id,team_idx))
        team = cursor.fetchone()
        if not team:
            return None, "تعذر العثور على فريقك في الجولة الحالية."
        cursor.execute("""SELECT m.user_id,COALESCE(s.xp_points,0),COALESCE(s.clip_points,0)
            FROM war_members m LEFT JOIN war_scores s
            ON s.guild_id=m.guild_id AND s.week_id=m.week_id AND s.user_id=m.user_id
            WHERE m.guild_id=? AND m.week_id=? AND m.team_idx=?""",(guild_id,week_id,team_idx))
        members=[{"id":uid,"xp":xp,"clip":clip,"total":xp+clip} for uid,xp,clip in cursor.fetchall()]
        members.sort(key=lambda member:(-member['total'],member['id']))
        cursor.execute("""SELECT t.team_idx,COALESCE(SUM(COALESCE(s.xp_points,0)+COALESCE(s.clip_points,0)),0)
            FROM war_teams t LEFT JOIN war_members m
            ON m.guild_id=t.guild_id AND m.week_id=t.week_id AND m.team_idx=t.team_idx
            LEFT JOIN war_scores s ON s.guild_id=m.guild_id AND s.week_id=m.week_id AND s.user_id=m.user_id
            WHERE t.guild_id=? AND t.week_id=? GROUP BY t.team_idx ORDER BY 2 DESC,t.team_idx ASC""",(guild_id,week_id))
        standings=cursor.fetchall()
    rank=next(index+1 for index,(idx,score) in enumerate(standings) if idx==team_idx)
    return {"week_id":week_id,"idx":team_idx,"name":team[0],"emoji":team[1],"leader_id":team[2],
            "members":members,"score":sum(m['total'] for m in members),
            "rank":rank,"team_count":len(standings)},None

async def build_personal_team_embed(guild,member,page=0):
    team,error=await asyncio.to_thread(war_personal_team_sync,guild.id,member.id)
    if error:
        return None,error,0,0
    members=team['members']
    page=max(0,min(page,(len(members)-1)//8))
    own=next(m for m in members if m['id']==member.id)
    percentage=own['total']/team['score']*100 if team['score'] else 0
    embed=discord.Embed(title=f"{team['emoji']} فريقك: {team['name']}"[:256],
        description=(f"الجولة **#{team['week_id']}** • ترتيب الفريق **{team['rank']}/{team['team_count']}**\n"
                     f"👑 القائد: <@{team['leader_id']}>\n"
                     f"🏆 مجموع الفريق: **{team['score']:,} نقطة**\n"
                     f"مساهمتك: **{own['total']:,} نقطة** — **{percentage:.1f}%** من مجموع الفريق"),
        color=discord.Color.from_rgb(255,215,0))
    for index,entry in enumerate(members[page*8:(page+1)*8],start=page*8+1):
        cached=guild.get_member(entry['id'])
        name=discord.utils.escape_markdown(cached.display_name) if cached else f"عضو {entry['id']}"
        leader=" 👑" if entry['id']==team['leader_id'] else ""
        you=" (أنت)" if entry['id']==member.id else ""
        share=entry['total']/team['score']*100 if team['score'] else 0
        embed.add_field(name=f"{index}. {name}{leader}{you}"[:256],
                        value=f"**{entry['total']:,} نقطة** • {share:.1f}%\n⭐ الخبرة: {entry['xp']:,} • 🎬 نقاط اللقطات: {entry['clip']:,}",inline=False)
    embed.set_footer(text=f"صفحة {page+1}/{max(1,(len(members)+7)//8)} • النقاط المعتمدة في نظام الفرق الحالي • عند التعادل يُرتّب الأعضاء بالمعرّف")
    return embed,None,page,len(members)

class PersonalTeamView(ButtonLimitedView):
    def __init__(self,owner_id,page=0,total=0):
        super().__init__(timeout=180)
        self.owner_id,self.page=owner_id,page
        self.previous.disabled=page<=0
        self.next.disabled=(page+1)*8>=total

    async def interaction_check(self,interaction):
        if interaction.user.id!=self.owner_id:
            await interaction.response.send_message("افتح لوحة فريقك من زر «فريقي».",ephemeral=True)
            return False
        return True

    async def update(self,interaction,page):
        await interaction.response.defer()
        embed,error,page,total=await build_personal_team_embed(interaction.guild,interaction.user,page)
        if error:
            await interaction.edit_original_response(content=error,embed=None,view=None)
        else:
            await interaction.edit_original_response(content=None,embed=embed,view=PersonalTeamView(self.owner_id,page,total),allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="السابق",style=discord.ButtonStyle.secondary)
    async def previous(self,interaction,button):
        await self.update(interaction,self.page-1)

    @discord.ui.button(label="تحديث",emoji="🔄",style=discord.ButtonStyle.primary)
    async def refresh(self,interaction,button):
        await self.update(interaction,self.page)

    @discord.ui.button(label="التالي",style=discord.ButtonStyle.secondary)
    async def next(self,interaction,button):
        await self.update(interaction,self.page+1)

async def show_personal_team(interaction):
    await interaction.response.defer(ephemeral=True)
    embed,error,page,total=await build_personal_team_embed(interaction.guild,interaction.user)
    if error:
        await interaction.followup.send(error,ephemeral=True)
    else:
        await interaction.followup.send(embed=embed,view=PersonalTeamView(interaction.user.id,page,total),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

@bot.tree.command(name="my_team",description="نقاط فريقك وترتيب أعضائه حسب مساهمتهم في حرب الفرق")
@app_commands.guild_only()
async def personal_team_command(interaction: discord.Interaction):
    await show_personal_team(interaction)


# ----------------- نظام المساعدة (Help Menu) -----------------
HELP_TOPICS = {
    "general": {
        "label": "⭐ أوامر عامة",
        "emoji": "⭐",
        "desc": "البروفايل، التلفيل، الاقتراحات",
        "title": "⭐ الأوامر العامة",
        "color": discord.Color.from_rgb(0, 168, 252),
        "text": (
            "**`/profile`** — عرض بروفايلك (أو بروفايل عضو ثاني): اللفل، الترتيب، تاريخ الانضمام وغيرها.\n\n"
            "**`/rank`** — عرض بطاقة مستواك ونقاط خبرتك كصورة.\n\n"
            "**`/suggest`** — إرسال اقتراح جديد للإدارة عبر نافذة كتابة سريعة.\n\n"
            "**`/set_birthday`** — تسجيل تاريخ ميلادك.\n\n"
            "**`/houses`** — عرض ترتيب البيوت الملكية بحرب الأسبوع.\n\n"
            "**`/steam_family_rules`** — عرض قوانين Steam Family."
        ),
    },
    "voice": {
        "label": "💬 الفويس المؤقت",
        "emoji": "💬",
        "desc": "إنشاء والتحكم بغرفتك الصوتية",
        "title": "💬 الرومات الصوتية المؤقتة",
        "color": discord.Color.from_rgb(87, 242, 135),
        "text": (
            "انضم إلى روم إنشاء فويس مؤقت وسيتم نقلك تلقائياً لغرفتك الخاصة.\n\n"
            "تظهر لك لوحة أزرار بشات الروم للتحكم الكامل: تغيير الاسم، تحديد عدد الأعضاء، "
            "قفل/فتح الغرفة، طرد أو حظر شخص، منح الثقة، ونقل الملكية.\n\n"
            "💡 _يتم حذف الروم تلقائياً فور خروج الجميع منه._"
        ),
    },
    "tickets": {
        "label": "🎫 الدعم الفني",
        "emoji": "🎫",
        "desc": "فتح تذكرة مساعدة مع الإدارة",
        "title": "🎫 نظام التذاكر",
        "color": discord.Color.from_rgb(237, 66, 69),
        "text": (
            "اضغط زر **الدعم الفني** بلوحة MEMBER CONTROL CENTER، اختر نوع طلبك، "
            "وراح يُفتح لك روم خاص يشوفه بس أنت والإدارة.\n\n"
            "🔒 تقدر تغلق التذكرة بنفسك بالضغط على زر الإغلاق داخل الروم عند الانتهاء."
        ),
    },
    "rl": {
        "label": "🎮 روكت ليق",
        "emoji": "🎮",
        "desc": "اختيار رتبة الرانك الخاصة بك",
        "title": "🎮 رتب روكت ليق",
        "color": discord.Color.from_rgb(241, 196, 15),
        "text": (
            "من اللوحة الأساسية اضغط زر اختيار الرانك لعرض رتب روكت ليق، بما فيها Grand Champion I وII وIII.\n"
            "اضغط زر رتبتك وراح تنعطى الرتبة تلقائياً (وتُزال أي رتبة قديمة)، "
            "واضغط عليها مرة ثانية لإزالتها بأي وقت."
        ),
    },
    "birthday": {
        "label": "🎂 أعياد الميلاد",
        "emoji": "🎂",
        "desc": "يهنئك البوت تلقائياً بيومك",
        "title": "🎂 نظام أعياد الميلاد",
        "color": discord.Color.from_rgb(255, 105, 180),
        "text": (
            "استخدم **`/set_birthday`** وحدد اليوم والشهر عشان يهنئك البوت تلقائياً بروم المناسبات بيومك.\n"
            "تقدر تحدّث تاريخك بإعادة استخدام نفس الأمر."
        ),
    },
    "quests": {
        "label": "🧭 المهام",
        "emoji": "🧭",
        "desc": "مهام يومية وأسبوعية بمكافآت",
        "title": "🧭 المهام اليومية والأسبوعية",
        "color": discord.Color.from_rgb(0, 168, 252),
        "text": (
            "اضغط زر **مهامي** بلوحة MEMBER CONTROL CENTER عشان تشوف مهامك الحالية وتقدمك فيها.\n\n"
            "☀️ **المهام اليومية** تتجدد كل يوم، و🗓️ **المهام الأسبوعية** تتجدد كل يوم جمعة.\n"
            "إنجاز أي مهمة يعطيك مكافأة نقاط خبرة (XP) تلقائياً فور اكتمالها، وتُعلن بروم الإعلانات."
        ),
    },
}

STAFF_HELP_TOPIC = {
    "label": "🛡️ أوامر الإدارة",
    "emoji": "🛡️",
    "desc": "أوامر خاصة بالطاقم والنائب والقائد",
    "title": "🛡️ أوامر الإدارة",
    "color": discord.Color.from_rgb(230, 126, 34),
    "text": (
        "**للنائب والقائد:**\n"
        "**اللوحات:** `/control_panel` `/setup_leader_voice_panel`\n"
        "**الإدارة:** `/lock` `/unlock` `/clear` `/embed` `/leaderboard`\n"
        "**النظام الملكي:** `/decree`\n"
        "**حرب البيوت:** `/royal_war_start` `/royal_war_end` `/start_teams` (للقائد فقط)\n\n"
        "**للقائد فقط:**\n"
        "**التلفيل:** `/add_xp` `/remove_xp` `/set_level` `/reset_level` `/fix_xp_desync` `/test_levelup` `/test_weekly_top`\n"
        "**اللوحات والنشر:** `/setup_roulette_panel` `/roulette` `/post_weekly_leaderboard` `/post_money_leaderboard`\n"
        "**النظام الملكي:** `/summon` `/royal_address` `/end_address`\n"
        "**صيانة:** `/pay_salaries_now` `/test_adhan` `/protect_current_role_holders` `/restore_missing_roles` `/diagnose_anniversaries` `/catch_up_anniversaries`\n"
        "**القوانين:** `/server_rules`"
    ),
}

class HelpTopicSelect(discord.ui.Select):
    def __init__(self, is_staff: bool):
        options = [
            discord.SelectOption(label=t["label"], description=t["desc"], emoji=t["emoji"], value=key)
            for key, t in HELP_TOPICS.items()
        ]
        if is_staff:
            options.append(discord.SelectOption(
                label=STAFF_HELP_TOPIC["label"], description=STAFF_HELP_TOPIC["desc"],
                emoji=STAFF_HELP_TOPIC["emoji"], value="staff"
            ))
        super().__init__(placeholder="اختر قسم المساعدة...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        topic = STAFF_HELP_TOPIC if self.values[0] == "staff" else HELP_TOPICS[self.values[0]]
        embed = discord.Embed(title=topic["title"], description=topic["text"], color=topic["color"])
        embed.set_image(url=BANNER_IMAGE_URL)
        apply_branding(embed, interaction.guild, user=interaction.user)
        await interaction.response.edit_message(embed=embed, view=self.view)

class HelpView(ButtonLimitedView):
    def __init__(self, is_staff: bool):
        super().__init__(timeout=180)
        self.add_item(HelpTopicSelect(is_staff))

# ----------------- نماذج المودال للتحكم بالفويس -----------------
class RenameVoiceModal(discord.ui.Modal, title="تغيير اسم القناة الصوتية"):
    name_input = discord.ui.TextInput(label="الاسم الجديد", placeholder="اكتب اسم القناة الجديد هنا...", min_length=1, max_length=50)

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.channel.edit(name=self.name_input.value)
        except Exception as e:
            await interaction.followup.send(f"❌ صار خطأ أثناء تغيير الاسم: {e}", ephemeral=True)
            return
        await interaction.followup.send(f"✅ تم تغيير اسم القناة إلى: `{self.name_input.value}`", ephemeral=True)

class LimitVoiceModal(discord.ui.Modal, title="تحديد عدد الأعضاء"):
    limit_input = discord.ui.TextInput(label="عدد الأعضاء (0 يعني بدون حد)", placeholder="0 - 99", min_length=1, max_length=2)

    def __init__(self, channel: discord.VoiceChannel, control_view: "TempVoiceControlView" = None):
        super().__init__()
        self.channel = channel
        self.control_view = control_view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(self.limit_input.value)
        except ValueError:
            await interaction.response.send_message("❌ يرجى إدخال رقم صحيح.", ephemeral=True)
            return

        if not (0 <= val <= 99):
            await interaction.response.send_message("❌ يرجى إدخال رقم بين 0 و 99.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await self.channel.edit(user_limit=val)
        except Exception as e:
            await interaction.followup.send(f"❌ صار خطأ أثناء ضبط الحد: {e}", ephemeral=True)
            return

        if self.control_view is not None:
            self.control_view.member_limit = val
            await self.control_view.refresh_status_display()

        limit_str = "بدون حد" if val == 0 else f"{val} أعضاء"
        await interaction.followup.send(f"✅ تم ضبط حد القناة إلى: `{limit_str}`", ephemeral=True)

# ----------------- نظام العملة: نافذة تحديد المبلغ + قائمة اختيار المستلم -----------------
class TransferAmountModal(discord.ui.Modal, title="تحويل رصيد"):
    amount_input = discord.ui.TextInput(label=f"المبلغ ({CURRENCY_NAME})", placeholder="مثال: 50", min_length=1, max_length=10)

    def __init__(self, sender_id: int, recipient_id: int, recipient_display: str):
        super().__init__()
        self.sender_id = sender_id
        self.recipient_id = recipient_id
        self.recipient_display = recipient_display

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount_input.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ يرجى إدخال رقم صحيح.", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message("❌ يرجى إدخال مبلغ أكبر من صفر.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        sender_balance = await get_balance(interaction.guild.id, self.sender_id)
        if sender_balance < amount:
            await interaction.followup.send(
                f"❌ رصيدك الحالي `{sender_balance:,} {CURRENCY_NAME}` وما يكفي لتحويل `{amount:,} {CURRENCY_NAME}`.",
                ephemeral=True
            )
            return

        new_sender_balance = await asyncio.to_thread(transfer_bank_sync, interaction.guild.id, self.sender_id, self.recipient_id, amount)
        if new_sender_balance is None:
            await interaction.followup.send("تعذر التحويل: تأكد من الرصيد والمستلم.", ephemeral=True)
            return

        recipient_member = interaction.guild.get_member(self.recipient_id)
        recipient_mention = recipient_member.mention if recipient_member else self.recipient_display

        await interaction.followup.send(
            f"✅ تم تحويل `{amount:,} {CURRENCY_NAME}` إلى {recipient_mention} بنجاح.\n"
            f"{CURRENCY_EMOJI} رصيدك الحالي: `{new_sender_balance:,} {CURRENCY_NAME}`",
            ephemeral=True
        )

        if recipient_member:
            try:
                new_recipient_balance = await get_balance(interaction.guild.id, self.recipient_id)
                dm_embed = discord.Embed(
                    title=f"{CURRENCY_EMOJI} وصلك تحويل جديد!",
                    description=(
                        f"حوّل لك {interaction.user.mention} رصيداً في سيرفر **{interaction.guild.name}**.\n\n"
                        f"➕ المبلغ المستلم: `{amount:,} {CURRENCY_NAME}`\n"
                        f"💼 رصيدك الحالي: `{new_recipient_balance:,} {CURRENCY_NAME}`"
                    ),
                    color=discord.Color.from_rgb(46, 204, 113)
                )
                apply_branding(dm_embed, interaction.guild, user=recipient_member)
                await send_optional_member_dm(recipient_member, recipient_member.guild.id, 'financial', embed=dm_embed)
            except Exception:
                pass  # الخاص مقفل عند المستلم أو صار خطأ، نتجاهل ونكمل عادي

        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            embed = discord.Embed(title="💸 تحويل رصيد بين الأعضاء", color=discord.Color.gold())
            embed.add_field(name="👤 من", value=f"<@{self.sender_id}>", inline=True)
            embed.add_field(name="👤 إلى", value=recipient_mention, inline=True)
            embed.add_field(name=f"{CURRENCY_EMOJI} المبلغ", value=f"`{amount:,} {CURRENCY_NAME}`", inline=True)
            apply_log_branding(embed, interaction.guild, target=interaction.user)
            try:
                await log_channel.send(embed=embed)
            except Exception:
                pass

class TransferRecipientSelect(discord.ui.UserSelect):
    def __init__(self, sender_id: int):
        super().__init__(placeholder="اختر الشخص المراد التحويل له...", min_values=1, max_values=1)
        self.sender_id = sender_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.sender_id:
            await interaction.response.send_message("❌ هذا التحويل ليس لك.", ephemeral=True)
            return

        recipient = self.values[0]
        if recipient.id == self.sender_id:
            await interaction.response.send_message("❌ ما تقدر تحول رصيد لنفسك.", ephemeral=True)
            return
        if recipient.bot:
            await interaction.response.send_message("❌ ما تقدر تحول رصيد لبوت.", ephemeral=True)
            return

        await interaction.response.send_modal(
            TransferAmountModal(sender_id=self.sender_id, recipient_id=recipient.id, recipient_display=str(recipient))
        )

class TransferRecipientView(ButtonLimitedView):
    def __init__(self, sender_id: int):
        super().__init__(timeout=60)
        self.add_item(TransferRecipientSelect(sender_id))

# ----------------- نافذة تأكيد عامة للإجراءات الخطيرة (حذف/حظر) -----------------
class ConfirmDangerView(ButtonLimitedView):
    def __init__(self, confirm_label: str, on_confirm, invoker_id: int, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.on_confirm = on_confirm
        self.invoker_id = invoker_id
        self.confirm_btn.label = confirm_label

    async def _deny_if_not_invoker(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("❌ هذا التأكيد ليس لك.", ephemeral=True)
            return True
        return False

    @discord.ui.button(label="نعم، تأكيد", emoji="✅", style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._deny_if_not_invoker(interaction):
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="⏳ جارِ التنفيذ...", view=self)
        try:
            await self.on_confirm(interaction)
        except Exception as e:
            print(f"[ConfirmDangerView] خطأ أثناء تنفيذ الإجراء المؤكد: {e}")

    @discord.ui.button(label="إلغاء", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._deny_if_not_invoker(interaction):
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="↩️ تم إلغاء العملية.", view=self)

# ----------------- واجهة التحكم بالفويس المؤقت -----------------
class TempVoiceControlView(ButtonLimitedView):
    def __init__(self, channel: discord.VoiceChannel, owner_id: int):
        super().__init__(timeout=None)
        self.channel = channel
        self.owner_id = owner_id
        self.is_locked = False
        self.is_hidden = False
        self.member_limit = 0
        # تُضبط من الخارج بعد الإنشاء: الإيمبيد الأساسي، إيمبيد الصورة التزيينية، ورسالة اللوحة نفسها
        self.base_embed: discord.Embed = None
        self.decor_embed: discord.Embed = None
        self.status_message: discord.Message = None

    def _status_text(self) -> str:
        lock_txt = "🔒 مقفولة" if self.is_locked else "🔓 مفتوحة"
        hide_txt = "🙈 مخفية" if self.is_hidden else "👁️ ظاهرة"
        limit_txt = f"👥 الحد: {self.member_limit}" if self.member_limit else "👥 بدون حد"
        return f"{lock_txt}   •   {hide_txt}   •   {limit_txt}"

    def _apply_status_to_embed(self):
        # لم تعد الحالة تُعرض كحقل ثابت بالإيمبيد (كانت تكبّر اللوحة بدون داعي)،
        # صارت تُعرض عند الطلب فقط عبر زر "📊 الحالة". الدالة باقية بدون تأثير
        # حتى ما نحتاج نعدّل كل مكان كانت تُستدعى فيه.
        pass

    async def refresh_status_display(self):
        """يحفظ تغييرات شكل الأزرار (مثل تبديل أيقونة القفل) على رسالة اللوحة مباشرة."""
        if self.status_message is None:
            return
        try:
            await self.status_message.edit(view=self)
        except Exception as e:
            print(f"[TempVoiceControlView] تعذّر تحديث لوحة الحالة: {e}")

    # ملاحظة: تم حذف interaction_check العام لأنه كان يمنع أي شخص غير
    # المالك من استخدام زر "استلام الملكية" (claim_btn)، وهذا الزر
    # بالتحديد يجب أن يستطيع أي عضو آخر بالروم استخدامه إذا غاب المالك.
    # بدلاً من ذلك، كل زر يتحقق من الملكية بنفسه عبر self._is_owner().

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def _deny_not_owner(self, interaction: discord.Interaction):
        await interaction.response.send_message("❌ أنت لست مالك هذه القناة الصوتية للتحكم بها.", ephemeral=True)

    # معالج أخطاء عام: أي استثناء غير متوقع بأي زر بهذي الواجهة يوصل هنا
    # بدل ما ديسكورد يعرض للمستخدم "This interaction failed" بدون أي توضيح
    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"[TempVoiceControlView] خطأ غير متوقع بزر '{getattr(item, 'emoji', item)}': {error}")
        error_text = "❌ صار خطأ غير متوقع أثناء تنفيذ العملية، حاول مرة ثانية بعد شوي."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(error_text, ephemeral=True)
            else:
                await interaction.response.send_message(error_text, ephemeral=True)
        except Exception:
            pass

    @discord.ui.button(emoji="💬", label="الدردشة", style=discord.ButtonStyle.secondary, row=0)
    async def chat_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction):
            return await self._deny_not_owner(interaction)
        await interaction.response.send_message("💬 يمكنك استخدام الشات النصي المدمج داخل القناة الصوتية مباشرة!", ephemeral=True)

    @discord.ui.button(emoji="⏱️", label="الانتظار", style=discord.ButtonStyle.secondary, row=0)
    async def waiting_room_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction):
            return await self._deny_not_owner(interaction)
        await interaction.response.send_message("⏱️ ميزة غرفة الانتظار مفعلة في الروم.", ephemeral=True)

    @discord.ui.button(emoji="🔒", label="قفل", style=discord.ButtonStyle.secondary, row=0)
    async def lock_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction):
            return await self._deny_not_owner(interaction)
        await interaction.response.defer()
        guild = interaction.guild
        new_state = not self.is_locked
        try:
            overwrite = self.channel.overwrites_for(guild.default_role)
            overwrite.connect = not new_state
            await self.channel.set_permissions(guild.default_role, overwrite=overwrite)
        except discord.Forbidden:
            await interaction.followup.send("❌ ما يملك البوت صلاحية تعديل صلاحيات هذا الروم.", ephemeral=True)
            return
        except Exception as e:
            print(f"[TempVoice] خطأ أثناء القفل/الفتح: {e}")
            await interaction.followup.send("❌ صار خطأ أثناء تنفيذ العملية، حاول مرة ثانية.", ephemeral=True)
            return

        self.is_locked = new_state
        button.emoji = "🔓" if self.is_locked else "🔒"
        button.label = "فتح" if self.is_locked else "قفل"
        self._apply_status_to_embed()
        status_text = "🔒 تم قفل القناة الصوتية (خاصة)" if self.is_locked else "🔓 تم فتح القناة الصوتية (عامة)"
        embeds = [self.decor_embed, self.base_embed] if self.decor_embed and self.base_embed else None
        if embeds:
            await interaction.edit_original_response(embeds=embeds, view=self)
        else:
            await interaction.edit_original_response(view=self)
        await interaction.followup.send(status_text, ephemeral=True)

    @discord.ui.button(emoji="👁️", label="إخفاء", style=discord.ButtonStyle.secondary, row=0)
    async def hide_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction):
            return await self._deny_not_owner(interaction)
        await interaction.response.defer()
        guild = interaction.guild
        new_state = not self.is_hidden
        try:
            overwrite = self.channel.overwrites_for(guild.default_role)
            overwrite.view_channel = not new_state
            await self.channel.set_permissions(guild.default_role, overwrite=overwrite)
        except discord.Forbidden:
            await interaction.followup.send("❌ ما يملك البوت صلاحية تعديل صلاحيات هذا الروم.", ephemeral=True)
            return
        except Exception as e:
            print(f"[TempVoice] خطأ أثناء الإخفاء/الإظهار: {e}")
            await interaction.followup.send("❌ صار خطأ أثناء تنفيذ العملية، حاول مرة ثانية.", ephemeral=True)
            return

        self.is_hidden = new_state
        button.emoji = "🙈" if self.is_hidden else "👁️"
        button.label = "إظهار" if self.is_hidden else "إخفاء"
        self._apply_status_to_embed()
        status_text = "👁️‍🗨️ تم إخفاء القناة الصوتية عن الجميع" if self.is_hidden else "👁️ تم إظهار القناة الصوتية للجميع"
        embeds = [self.decor_embed, self.base_embed] if self.decor_embed and self.base_embed else None
        if embeds:
            await interaction.edit_original_response(embeds=embeds, view=self)
        else:
            await interaction.edit_original_response(view=self)
        await interaction.followup.send(status_text, ephemeral=True)

    @discord.ui.button(emoji="✏️", label="الاسم", style=discord.ButtonStyle.secondary, row=1)
    async def rename_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction):
            return await self._deny_not_owner(interaction)
        await interaction.response.send_modal(RenameVoiceModal(self.channel))

    @discord.ui.button(emoji="👥", label="الحد", style=discord.ButtonStyle.secondary, row=1)
    async def limit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction):
            return await self._deny_not_owner(interaction)
        await interaction.response.send_modal(LimitVoiceModal(self.channel, self))

    @discord.ui.button(emoji="🌐", label="السيرفر", style=discord.ButtonStyle.secondary, row=1)
    async def region_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction):
            return await self._deny_not_owner(interaction)
        await interaction.response.send_message("🌐 ريجين القناة مضبوط تلقائياً على أفضل اتصال (Automatic).", ephemeral=True)

    @discord.ui.button(emoji="🚫", label="طرد", style=discord.ButtonStyle.secondary, row=2)
    async def kick_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction):
            return await self._deny_not_owner(interaction)
        other_members = [m for m in self.channel.members if m.id != self.owner_id]
        if not other_members:
            await interaction.response.send_message("❌ لا يوجد أعضاء آخرين في الروم لطردهم.", ephemeral=True)
            return
        select_view = ButtonLimitedView()
        select = discord.ui.Select(placeholder="اختر العضو المراد طرده من الروم...")
        for m in other_members[:25]:
            select.add_option(label=m.display_name, value=str(m.id))

        async def select_callback(select_interaction: discord.Interaction):
            await select_interaction.response.defer(ephemeral=True)
            target_id = int(select.values[0])
            target_member = interaction.guild.get_member(target_id)
            if target_member and target_member.voice and target_member.voice.channel == self.channel:
                await target_member.move_to(None)
                await select_interaction.followup.send(f"✅ تم طرد {target_member.mention} من الروم.", ephemeral=True)
            else:
                await select_interaction.followup.send("❌ العضو غير متواجد بالروم.", ephemeral=True)

        select.callback = select_callback
        select_view.add_item(select)
        await interaction.response.send_message("اختر العضو المراد طرده:", view=select_view, ephemeral=True)

    @discord.ui.button(emoji="➕", label="دعوة", style=discord.ButtonStyle.secondary, row=2)
    async def invite_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction):
            return await self._deny_not_owner(interaction)
        await interaction.response.defer(ephemeral=True)
        try:
            invite = await self.channel.create_invite(max_age=3600, max_uses=5)
        except discord.Forbidden:
            await interaction.followup.send("❌ ما يملك البوت صلاحية إنشاء دعوة لهذا الروم.", ephemeral=True)
            return
        except Exception as e:
            print(f"[TempVoice] خطأ أثناء إنشاء الدعوة: {e}")
            await interaction.followup.send("❌ صار خطأ أثناء إنشاء الدعوة، حاول مرة ثانية.", ephemeral=True)
            return
        await interaction.followup.send(f"🔗 رابط دعوة مؤقت لرومك الصوتي:\n{invite.url}", ephemeral=True)

    @discord.ui.button(emoji="⭐", label="ثقة", style=discord.ButtonStyle.secondary, row=1)
    async def trust_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction):
            return await self._deny_not_owner(interaction)
        await interaction.response.send_message("⭐ ميزة منح الثقة للأصدقاء مفعلة.", ephemeral=True)

    @discord.ui.button(emoji="🗑️", label="حذف", style=discord.ButtonStyle.danger, row=3)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction):
            return await self._deny_not_owner(interaction)

        async def _do_delete(confirm_interaction: discord.Interaction):
            temp_channels.pop(self.channel.id, None)
            try:
                await self.channel.delete()
            except Exception as e:
                print(f"[TempVoice] خطأ أثناء حذف القناة: {e}")

        confirm_view = ConfirmDangerView("نعم، احذف القناة", _do_delete, interaction.user.id)
        await interaction.response.send_message(
            "⚠️ متأكد إنك تبي تحذف القناة الصوتية نهائيًا؟ هذا الإجراء ما يرجع.",
            view=confirm_view, ephemeral=True
        )

    @discord.ui.button(emoji="👑", label="نقل الملكية", style=discord.ButtonStyle.secondary, row=3)
    async def transfer_owner_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction):
            return await self._deny_not_owner(interaction)
        other_members = [m for m in self.channel.members if m.id != self.owner_id]
        if not other_members:
            await interaction.response.send_message("❌ لا يوجد عضو آخر في الروم لنقل الملكية إليه.", ephemeral=True)
            return

        select_view = ButtonLimitedView()
        select = discord.ui.Select(placeholder="اختر المالك الجديد للروم...")
        for m in other_members[:25]:
            select.add_option(label=m.display_name, value=str(m.id))

        async def select_callback(select_interaction: discord.Interaction):
            new_owner_id = int(select.values[0])
            self.owner_id = new_owner_id
            temp_channels[self.channel.id] = new_owner_id
            new_owner = interaction.guild.get_member(new_owner_id)
            await select_interaction.response.send_message(f"👑 تم نقل ملكية القناة الصوتية إلى {new_owner.mention}!", ephemeral=False)

        select.callback = select_callback
        select_view.add_item(select)
        await interaction.response.send_message("اختر العضو لنقل الملكية إليه:", view=select_view, ephemeral=True)

    # هذا الزر مقصود أن يستخدمه أي عضو (وليس المالك فقط)، لذلك لا يوجد فيه فحص ملكية
    @discord.ui.button(emoji="🤝", label="استلام", style=discord.ButtonStyle.secondary, row=3)
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        owner_member = interaction.guild.get_member(self.owner_id)
        if not owner_member or owner_member not in self.channel.members:
            self.owner_id = interaction.user.id
            temp_channels[self.channel.id] = interaction.user.id
            await interaction.response.send_message(f"👑 تم استلام ملكية الروم الصوتي بواسطة {interaction.user.mention}!", ephemeral=False)
        else:
            await interaction.response.send_message("❌ مالك الروم متواجد حالياً، لا يمكنك استلام الملكية.", ephemeral=True)

    # زر يعرض الحالة الحالية للروم (بدل ما كانت تُعرض دائمًا كحقل ثابت بالإيمبيد)
    @discord.ui.button(emoji="📊", label="الحالة", style=discord.ButtonStyle.secondary, row=3)
    async def status_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(f"📊 **الحالة الحالية:**\n{self._status_text()}", ephemeral=True)

    @discord.ui.button(emoji="🔓", label="فك حظر", style=discord.ButtonStyle.secondary, row=2)
    async def unban_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction):
            return await self._deny_not_owner(interaction)

        # جلب كل الأعضاء الممنوعين فعلياً من الدخول (connect=False) في هذه القناة
        banned_members = [
            m for m, ov in self.channel.overwrites.items()
            if isinstance(m, discord.Member) and ov.connect is False
        ]

        if not banned_members:
            await interaction.response.send_message("❌ لا يوجد أعضاء محظورين حالياً من هذا الروم.", ephemeral=True)
            return

        select_view = ButtonLimitedView()
        select = discord.ui.Select(placeholder="اختر العضو المراد فك حظره من الروم...")
        for m in banned_members[:25]:
            select.add_option(label=m.display_name, value=str(m.id))

        async def select_callback(select_interaction: discord.Interaction):
            await select_interaction.response.defer(ephemeral=True)
            target_id = int(select.values[0])
            target_member = interaction.guild.get_member(target_id)
            if target_member:
                try:
                    await self.channel.set_permissions(target_member, overwrite=None)
                except discord.Forbidden:
                    await select_interaction.followup.send("❌ ما يملك البوت صلاحية تعديل صلاحيات هذا الروم.", ephemeral=True)
                    return
                except Exception as e:
                    print(f"[TempVoice] خطأ أثناء فك الحظر: {e}")
                    await select_interaction.followup.send("❌ صار خطأ أثناء تنفيذ العملية.", ephemeral=True)
                    return
                await select_interaction.followup.send(f"🔓 تم فك الحظر عن {target_member.mention} ويمكنه الآن دخول الروم.", ephemeral=True)

        select.callback = select_callback
        select_view.add_item(select)
        await interaction.response.send_message("اختر العضو المراد فك حظره:", view=select_view, ephemeral=True)

    @discord.ui.button(emoji="⛔", label="حظر", style=discord.ButtonStyle.secondary, row=2)
    async def ban_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction):
            return await self._deny_not_owner(interaction)
        other_members = [m for m in self.channel.members if m.id != self.owner_id]
        if not other_members:
            await interaction.response.send_message("❌ لا يوجد أعضاء بالروم لحظره.", ephemeral=True)
            return

        select_view = ButtonLimitedView()
        select = discord.ui.Select(placeholder="اختر العضو المراد منعه من دخول الروم...")
        for m in other_members[:25]:
            select.add_option(label=m.display_name, value=str(m.id))

        async def select_callback(select_interaction: discord.Interaction):
            target_id = int(select.values[0])
            target_member = interaction.guild.get_member(target_id)
            if not target_member:
                await select_interaction.response.send_message("❌ تعذّر إيجاد العضو.", ephemeral=True)
                return

            async def _do_ban(confirm_interaction: discord.Interaction):
                try:
                    overwrite = self.channel.overwrites_for(target_member)
                    overwrite.connect = False
                    await self.channel.set_permissions(target_member, overwrite=overwrite)
                    if target_member.voice and target_member.voice.channel == self.channel:
                        await target_member.move_to(None)
                except discord.Forbidden:
                    await confirm_interaction.followup.send("❌ ما يملك البوت صلاحية تعديل صلاحيات هذا الروم.", ephemeral=True)
                    return
                except Exception as e:
                    print(f"[TempVoice] خطأ أثناء الحظر: {e}")
                    await confirm_interaction.followup.send("❌ صار خطأ أثناء تنفيذ العملية.", ephemeral=True)
                    return
                await confirm_interaction.followup.send(f"⛔ تم حظر {target_member.mention} ومنعه من دخول الروم.", ephemeral=True)

            confirm_view = ConfirmDangerView(f"نعم، احظر {target_member.display_name}", _do_ban, select_interaction.user.id)
            await select_interaction.response.send_message(
                f"⚠️ متأكد إنك تبي تحظر {target_member.mention} من الروم؟",
                view=confirm_view, ephemeral=True
            )

        select.callback = select_callback
        select_view.add_item(select)
        await interaction.response.send_message("اختر العضو المراد حظره:", view=select_view, ephemeral=True)

    @discord.ui.button(emoji="❓", label="مساعدة", style=discord.ButtonStyle.primary, row=4)
    async def help_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guide_buffer = await create_temp_voice_guide_image()
        guide_file = discord.File(fp=guide_buffer, filename="voice_controls.png")
        help_embed = discord.Embed(
            title="❓ شرح أزرار التحكم",
            description="هذي صورة توضح وظيفة كل زر بلوحة التحكم بالتفصيل:",
            color=discord.Color.from_rgb(0, 168, 252)
        )
        help_embed.set_image(url="attachment://voice_controls.png")
        await interaction.followup.send(embed=help_embed, file=guide_file, ephemeral=True)

# ----------------- واجهة التحكم الصوتية لرومات ثابتة (القائد والنائب فقط) -----------------
# عكس TempVoiceControlView (رومات مؤقتة تُنشأ وتُحذف تلقائياً)، هذه اللوحة
# تُرسل مرة واحدة برومات صوتية ثابتة (محددة بـ LEADER_DEPUTY_VOICE_PANEL_CHANNEL_IDS)
# وتبقى صالحة للاستخدام دائماً - حتى بعد إعادة تشغيل البوت - بفضل استخدام
# custom_id ثابت مبني على رقم القناة (بدل الاعتماد على استمرار الرسالة بالذاكرة).
class LeaderDeputyVoiceControlView(ButtonLimitedView):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        self.is_locked = False
        self.is_hidden = False
        self.is_address_active = False
        self.member_limit = 0
        self._lock_button = None
        self._hide_button = None
        self._address_button = None
        # تُضبط من الخارج بعد الإنشاء (setup_leader_voice_panel): الإيمبيد الأساسي، إيمبيد الصورة التزيينية، ورسالة اللوحة نفسها
        self.base_embed: discord.Embed = None
        self.decor_embed: discord.Embed = None
        self.status_message: discord.Message = None
        self._build_buttons()

    def _get_channel(self, interaction: discord.Interaction):
        return interaction.guild.get_channel(self.channel_id)

    def _is_authorized(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        return is_staff_member(interaction.user)

    async def _deny(self, interaction: discord.Interaction):
        await interaction.response.send_message("❌ هذه اللوحة مخصصة لصاحب السيرفر (القائد) والنائب فقط.", ephemeral=True)

    def _status_text(self) -> str:
        lock_txt = "🔒 مقفولة" if self.is_locked else "🔓 مفتوحة"
        hide_txt = "🙈 مخفية" if self.is_hidden else "👁️ ظاهرة"
        limit_txt = f"👥 الحد: {self.member_limit}" if self.member_limit else "👥 بدون حد"
        address_txt = "📢 الخطاب الملكي: نشط" if self.is_address_active else "📢 الخطاب الملكي: متوقف"
        return f"{lock_txt}   •   {hide_txt}   •   {limit_txt}\n{address_txt}"

    def _apply_status_to_embed(self):
        # لم تعد الحالة تُعرض كحقل ثابت بالإيمبيد، صارت عبر زر "📊 الحالة" عند الطلب.
        # الدالة باقية بدون تأثير حتى ما نحتاج نعدّل كل مكان كانت تُستدعى فيه.
        pass

    async def refresh_status_display(self):
        """يحفظ تغييرات شكل الأزرار (مثل تبديل أيقونة القفل) على رسالة اللوحة مباشرة."""
        if self.status_message is None:
            return
        try:
            await self.status_message.edit(view=self)
        except Exception as e:
            print(f"[LeaderDeputyVoiceControlView] تعذّر تحديث لوحة الحالة: {e}")

    # معالج أخطاء عام لأي استثناء غير متوقع بأي زر بهذي الواجهة
    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"[LeaderDeputyVoiceControlView] خطأ غير متوقع بزر '{getattr(item, 'emoji', item)}': {error}")
        error_text = "❌ صار خطأ غير متوقع أثناء تنفيذ العملية، حاول مرة ثانية بعد شوي."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(error_text, ephemeral=True)
            else:
                await interaction.response.send_message(error_text, ephemeral=True)
        except Exception:
            pass

    def _build_buttons(self):
        # (الإيموجي، اسم الإجراء الداخلي، رقم الصف، ستايل الزر، النص)
        # ملاحظة: أزرار "استدعاء / خطاب ملكي / مرسوم" هي أوامر القائد الملكية الحصرية لذا لونها أزرق (primary)
        specs = [
            ("💬", "chat", 0, discord.ButtonStyle.primary, "الدردشة"),
            ("⏱️", "wait", 0, discord.ButtonStyle.primary, "الانتظار"),
            ("🔒", "lock", 0, discord.ButtonStyle.primary, "قفل"),
            ("👁️", "hide", 0, discord.ButtonStyle.primary, "إخفاء"),
            ("✏️", "rename", 1, discord.ButtonStyle.primary, "الاسم"),
            ("👥", "limit", 1, discord.ButtonStyle.primary, "الحد"),
            ("🌐", "region", 1, discord.ButtonStyle.primary, "السيرفر"),
            ("⭐", "trust", 1, discord.ButtonStyle.primary, "الثقة"),
            ("🚫", "kick", 2, discord.ButtonStyle.primary, "طرد"),
            ("➕", "invite", 2, discord.ButtonStyle.primary, "دعوة"),
            ("⛔", "ban", 2, discord.ButtonStyle.danger, "حظر"),
            ("🔓", "unban", 2, discord.ButtonStyle.primary, "فك حظر"),
            ("👑", "summon", 3, discord.ButtonStyle.primary, "استدعاء"),
            ("📢", "address", 3, discord.ButtonStyle.primary, "خطاب ملكي"),
            ("📜", "decree", 3, discord.ButtonStyle.primary, "مرسوم"),
            ("❓", "help", 4, discord.ButtonStyle.primary, "مساعدة"),
        ]
        for emoji, action, row, style, label in specs:
            btn = discord.ui.Button(
                emoji=emoji,
                label=label,
                style=style,
                row=row,
                custom_id=f"ldv:{action}:{self.channel_id}",
            )
            btn.callback = self._make_callback(action)
            self.add_item(btn)
            if action == "lock":
                self._lock_button = btn
            elif action == "hide":
                self._hide_button = btn
            elif action == "address":
                self._address_button = btn

    def _make_callback(self, action: str):
        handler = getattr(self, f"_handle_{action}")

        async def callback(interaction: discord.Interaction):
            if not self._is_authorized(interaction):
                return await self._deny(interaction)
            channel = self._get_channel(interaction)
            if channel is None:
                return await interaction.response.send_message("❌ تعذر العثور على القناة الصوتية.", ephemeral=True)
            await handler(interaction, channel)

        return callback

    # ---- أزرار معلوماتية (نفس فكرة واجهة الفويس المؤقت) ----
    async def _handle_chat(self, interaction: discord.Interaction, channel):
        await interaction.response.send_message("💬 يمكنك استخدام الشات النصي المدمج داخل القناة الصوتية مباشرة!", ephemeral=True)

    async def _handle_wait(self, interaction: discord.Interaction, channel):
        await interaction.response.send_message("⏱️ ميزة غرفة الانتظار مفعلة في الروم.", ephemeral=True)

    async def _handle_region(self, interaction: discord.Interaction, channel):
        await interaction.response.send_message("🌐 ريجين القناة مضبوط تلقائياً على أفضل اتصال (Automatic).", ephemeral=True)

    async def _handle_trust(self, interaction: discord.Interaction, channel):
        await interaction.response.send_message("⭐ ميزة منح الثقة للأصدقاء مفعلة.", ephemeral=True)

    # ---- الخصوصية: قفل/فتح دخول الروم للجميع ----
    async def _handle_lock(self, interaction: discord.Interaction, channel):
        await interaction.response.defer()
        guild = interaction.guild
        new_state = not self.is_locked
        try:
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.connect = not new_state
            await channel.set_permissions(guild.default_role, overwrite=overwrite)
        except discord.Forbidden:
            await interaction.followup.send("❌ ما يملك البوت صلاحية تعديل صلاحيات هذا الروم.", ephemeral=True)
            return
        except Exception as e:
            print(f"[LeaderDeputyVoiceControlView] خطأ أثناء القفل/الفتح: {e}")
            await interaction.followup.send("❌ صار خطأ أثناء تنفيذ العملية، حاول مرة ثانية.", ephemeral=True)
            return

        self.is_locked = new_state
        if self._lock_button:
            self._lock_button.emoji = "🔓" if self.is_locked else "🔒"
            self._lock_button.label = "فتح" if self.is_locked else "قفل"
        self._apply_status_to_embed()
        status_text = "🔒 تم قفل القناة الصوتية (خاصة)" if self.is_locked else "🔓 تم فتح القناة الصوتية (عامة)"
        embeds = [self.decor_embed, self.base_embed] if self.decor_embed and self.base_embed else None
        if embeds:
            await interaction.edit_original_response(embeds=embeds, view=self)
        elif self.base_embed is not None:
            await interaction.edit_original_response(embed=self.base_embed, view=self)
        else:
            await interaction.edit_original_response(view=self)
        await interaction.followup.send(status_text, ephemeral=True)

    # ---- إخفاء/إظهار الروم عن الجميع ----
    async def _handle_hide(self, interaction: discord.Interaction, channel):
        await interaction.response.defer()
        guild = interaction.guild
        new_state = not self.is_hidden
        try:
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.view_channel = not new_state
            await channel.set_permissions(guild.default_role, overwrite=overwrite)
        except discord.Forbidden:
            await interaction.followup.send("❌ ما يملك البوت صلاحية تعديل صلاحيات هذا الروم.", ephemeral=True)
            return
        except Exception as e:
            print(f"[LeaderDeputyVoiceControlView] خطأ أثناء الإخفاء/الإظهار: {e}")
            await interaction.followup.send("❌ صار خطأ أثناء تنفيذ العملية، حاول مرة ثانية.", ephemeral=True)
            return

        self.is_hidden = new_state
        if self._hide_button:
            self._hide_button.emoji = "🙈" if self.is_hidden else "👁️"
            self._hide_button.label = "إظهار" if self.is_hidden else "إخفاء"
        self._apply_status_to_embed()
        status_text = "👁️‍🗨️ تم إخفاء القناة الصوتية عن الجميع" if self.is_hidden else "👁️ تم إظهار القناة الصوتية للجميع"
        embeds = [self.decor_embed, self.base_embed] if self.decor_embed and self.base_embed else None
        if embeds:
            await interaction.edit_original_response(embeds=embeds, view=self)
        elif self.base_embed is not None:
            await interaction.edit_original_response(embed=self.base_embed, view=self)
        else:
            await interaction.edit_original_response(view=self)
        await interaction.followup.send(status_text, ephemeral=True)

    # ---- تغيير الاسم ----
    async def _handle_rename(self, interaction: discord.Interaction, channel):
        await interaction.response.send_modal(RenameVoiceModal(channel))

    # ---- تحديد عدد الأعضاء (الحد) ----
    async def _handle_limit(self, interaction: discord.Interaction, channel):
        await interaction.response.send_modal(LimitVoiceModal(channel, self))

    # ---- طرد عضو من الروم ----
    async def _handle_kick(self, interaction: discord.Interaction, channel):
        other_members = [m for m in channel.members if not m.bot]
        if not other_members:
            await interaction.response.send_message("❌ لا يوجد أعضاء بالروم لطردهم.", ephemeral=True)
            return

        select_view = ButtonLimitedView()
        select = discord.ui.Select(placeholder="اختر العضو المراد طرده من الروم...")
        for m in other_members[:25]:
            select.add_option(label=m.display_name, value=str(m.id))

        async def select_callback(select_interaction: discord.Interaction):
            await select_interaction.response.defer(ephemeral=True)
            target_id = int(select.values[0])
            target_member = select_interaction.guild.get_member(target_id)
            if target_member and target_member.voice and target_member.voice.channel and target_member.voice.channel.id == channel.id:
                await target_member.move_to(None)
                await select_interaction.followup.send(f"✅ تم طرد {target_member.mention} من الروم.", ephemeral=True)
            else:
                await select_interaction.followup.send("❌ العضو غير متواجد بالروم.", ephemeral=True)

        select.callback = select_callback
        select_view.add_item(select)
        await interaction.response.send_message("اختر العضو المراد طرده:", view=select_view, ephemeral=True)

    # ---- إنشاء دعوة مؤقتة للروم ----
    async def _handle_invite(self, interaction: discord.Interaction, channel):
        await interaction.response.defer(ephemeral=True)
        try:
            invite = await channel.create_invite(max_age=3600, max_uses=5)
        except discord.Forbidden:
            await interaction.followup.send("❌ ما يملك البوت صلاحية إنشاء دعوة لهذا الروم.", ephemeral=True)
            return
        except Exception as e:
            print(f"[LeaderDeputyVoiceControlView] خطأ أثناء إنشاء الدعوة: {e}")
            await interaction.followup.send("❌ صار خطأ أثناء إنشاء الدعوة، حاول مرة ثانية.", ephemeral=True)
            return
        await interaction.followup.send(f"🔗 رابط دعوة مؤقت لهذا الروم الصوتي:\n{invite.url}", ephemeral=True)

    # ---- حظر عضو من دخول الروم ----
    async def _handle_ban(self, interaction: discord.Interaction, channel):
        other_members = [m for m in channel.members if not m.bot]
        if not other_members:
            await interaction.response.send_message("❌ لا يوجد أعضاء بالروم لحظرهم.", ephemeral=True)
            return

        select_view = ButtonLimitedView()
        select = discord.ui.Select(placeholder="اختر العضو المراد منعه من دخول الروم...")
        for m in other_members[:25]:
            select.add_option(label=m.display_name, value=str(m.id))

        async def select_callback(select_interaction: discord.Interaction):
            target_id = int(select.values[0])
            target_member = select_interaction.guild.get_member(target_id)
            if not target_member:
                await select_interaction.response.send_message("❌ تعذّر إيجاد العضو.", ephemeral=True)
                return

            async def _do_ban(confirm_interaction: discord.Interaction):
                try:
                    overwrite = channel.overwrites_for(target_member)
                    overwrite.connect = False
                    await channel.set_permissions(target_member, overwrite=overwrite)
                    if target_member.voice and target_member.voice.channel and target_member.voice.channel.id == channel.id:
                        await target_member.move_to(None)
                except discord.Forbidden:
                    await confirm_interaction.followup.send("❌ ما يملك البوت صلاحية تعديل صلاحيات هذا الروم.", ephemeral=True)
                    return
                except Exception as e:
                    print(f"[LeaderDeputyVoiceControlView] خطأ أثناء الحظر: {e}")
                    await confirm_interaction.followup.send("❌ صار خطأ أثناء تنفيذ العملية.", ephemeral=True)
                    return
                await confirm_interaction.followup.send(f"⛔ تم حظر {target_member.mention} ومنعه من دخول الروم.", ephemeral=True)

            confirm_view = ConfirmDangerView(f"نعم، احظر {target_member.display_name}", _do_ban, select_interaction.user.id)
            await select_interaction.response.send_message(
                f"⚠️ متأكد إنك تبي تحظر {target_member.mention} من الروم؟",
                view=confirm_view, ephemeral=True
            )

        select.callback = select_callback
        select_view.add_item(select)
        await interaction.response.send_message("اختر العضو المراد حظره:", view=select_view, ephemeral=True)

    # ---- فك الحظر عن عضو ----
    async def _handle_unban(self, interaction: discord.Interaction, channel):
        banned_members = [
            m for m, ov in channel.overwrites.items()
            if isinstance(m, discord.Member) and ov.connect is False
        ]
        if not banned_members:
            await interaction.response.send_message("❌ لا يوجد أعضاء محظورين حالياً من هذا الروم.", ephemeral=True)
            return

        select_view = ButtonLimitedView()
        select = discord.ui.Select(placeholder="اختر العضو المراد فك حظره من الروم...")
        for m in banned_members[:25]:
            select.add_option(label=m.display_name, value=str(m.id))

        async def select_callback(select_interaction: discord.Interaction):
            await select_interaction.response.defer(ephemeral=True)
            target_id = int(select.values[0])
            target_member = select_interaction.guild.get_member(target_id)
            if target_member:
                try:
                    await channel.set_permissions(target_member, overwrite=None)
                except discord.Forbidden:
                    await select_interaction.followup.send("❌ ما يملك البوت صلاحية تعديل صلاحيات هذا الروم.", ephemeral=True)
                    return
                except Exception as e:
                    print(f"[LeaderDeputyVoiceControlView] خطأ أثناء فك الحظر: {e}")
                    await select_interaction.followup.send("❌ صار خطأ أثناء تنفيذ العملية.", ephemeral=True)
                    return
                await select_interaction.followup.send(f"🔓 تم فك الحظر عن {target_member.mention} ويمكنه الآن دخول الروم.", ephemeral=True)

        select.callback = select_callback
        select_view.add_item(select)
        await interaction.response.send_message("اختر العضو المراد فك حظره:", view=select_view, ephemeral=True)

    # ---- الاستدعاء الملكي: نقل عضو متواجد بروم صوتي إلى هذا الروم ----
    async def _handle_summon(self, interaction: discord.Interaction, channel):
        select_view = SummonSelectView(requester_id=interaction.user.id, target_channel=channel)
        await interaction.response.send_message(
            "👑 اختر العضو المطلوب استدعاؤه إلى هذا الروم:",
            view=select_view,
            ephemeral=True
        )

    # ---- الخطاب الملكي: تبديل بين بدء الخطاب وإنهائه بهذا الروم ----
    async def _handle_address(self, interaction: discord.Interaction, channel):
        await interaction.response.defer()
        member = interaction.user
        if channel.id in royal_address_active:
            success = await execute_royal_address_end(interaction, channel, member)
            if success:
                self.is_address_active = False
                if self._address_button:
                    self._address_button.emoji = "📢"
                self._apply_status_to_embed()
                await self._resend_panel_view(interaction)
        else:
            success = await execute_royal_address_start(interaction, channel, member)
            if success:
                self.is_address_active = True
                if self._address_button:
                    self._address_button.emoji = "🔇"
                self._apply_status_to_embed()
                await self._resend_panel_view(interaction)

    async def _resend_panel_view(self, interaction: discord.Interaction):
        """يحدّث رسالة اللوحة بعد تغيير شكل زر، مع الحفاظ على صورة الـ GIF التزيينية إن وُجدت."""
        embeds = [self.decor_embed, self.base_embed] if self.decor_embed and self.base_embed else None
        if embeds:
            await interaction.edit_original_response(embeds=embeds, view=self)
        elif self.base_embed is not None:
            await interaction.edit_original_response(embed=self.base_embed, view=self)
        else:
            await interaction.edit_original_response(view=self)

    # ---- إصدار مرسوم رسمي ----
    async def _handle_decree(self, interaction: discord.Interaction, channel):
        await interaction.response.send_modal(DecreeModal())

    # ---- مساعدة: شرح الأزرار بالتفصيل بصورة ----
    async def _handle_help(self, interaction: discord.Interaction, channel):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guide_buffer = await create_leader_voice_guide_image()
        guide_file = discord.File(fp=guide_buffer, filename="leader_voice_controls.png")
        help_embed = discord.Embed(
            title="❓ شرح أزرار لوحة القائد والنائب",
            description="هذي صورة توضح وظيفة كل زر بلوحة التحكم بالتفصيل:",
            color=discord.Color.from_rgb(0, 168, 252)
        )
        help_embed.set_image(url="attachment://leader_voice_controls.png")
        await interaction.followup.send(embed=help_embed, file=guide_file, ephemeral=True)

# ----------------- نماذج وقوائم أزرار الاستدعاء والمرسوم -----------------
class SummonReasonModal(discord.ui.Modal, title="سبب الاستدعاء الملكي"):
    reason_input = discord.ui.TextInput(
        label="سبب الاستدعاء (اختياري)",
        placeholder="اكتب سبب الاستدعاء هنا...",
        required=False,
        max_length=200
    )

    def __init__(self, member: discord.Member, channel: discord.VoiceChannel):
        super().__init__()
        self.member = member
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await execute_summon(interaction, self.member, self.channel, self.reason_input.value or None)

class SummonSelectView(ButtonLimitedView):
    def __init__(self, requester_id: int, target_channel: discord.VoiceChannel):
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.target_channel = target_channel
        self.selected_member = None

        self.member_select = discord.ui.UserSelect(
            placeholder="اختر العضو المطلوب استدعاؤه...",
            min_values=1,
            max_values=1
        )
        self.member_select.callback = self._on_member_select
        self.add_item(self.member_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("❌ هذه القائمة ليست لك.", ephemeral=True)
            return False
        return True

    async def _on_member_select(self, interaction: discord.Interaction):
        selected = self.member_select.values[0]
        member = interaction.guild.get_member(selected.id)
        if member is None:
            await interaction.response.send_message("❌ تعذر العثور على هذا العضو بالسيرفر.", ephemeral=True)
            return
        self.selected_member = member
        await interaction.response.send_modal(SummonReasonModal(member, self.target_channel))

# ----------------- نموذج إصدار مرسوم رسمي -----------------
class DecreeModal(discord.ui.Modal, title="إصدار مرسوم رسمي"):
    title_input = discord.ui.TextInput(
        label="عنوان المرسوم",
        placeholder="اكتب عنوان المرسوم هنا...",
        max_length=200
    )
    content_input = discord.ui.TextInput(
        label="نص المرسوم الكامل",
        style=discord.TextStyle.paragraph,
        placeholder="اكتب نص المرسوم هنا...",
        max_length=3500
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await execute_decree(interaction, self.title_input.value, self.content_input.value)

# ----------------- رسم صورة المتصدرين LEADERBOARD -----------------
# tier ألوان أول 3 مراكز (ذهبي / فضي / برونزي) — تُستخدم لشارة الترتيب وحلقة الصورة وخط التمييز الجانبي
RANK_TIERS = {
    1: {"accent": (255, 209, 102), "glow": (255, 209, 102, 26)},
    2: {"accent": (198, 202, 214), "glow": (198, 202, 214, 20)},
    3: {"accent": (222, 148, 92),  "glow": (222, 148, 92, 24)},
}
DEFAULT_TIER = {"accent": (90, 96, 110), "glow": None}

def render_leaderboard_card_sync(guild_name: str, top_users_data: list, mode: str = "alltime", money_stats: dict = None) -> io.BytesIO:
    """
    top_users_data: لائحة من tuples بالشكل:
        (rank_num, username, lvl, xp, needed_xp, avatar_bytes, weekly_xp)
    mode: "alltime" يعرض إجمالي الـ XP، "weekly" يعرض XP هذا الأسبوع فقط، و"money" يعرض أعلى الأرصدة
          (يُمرَّر الرصيد بحقل weekly_xp بالـ tuple بهذا الوضع).
    money_stats: بوضع "money" فقط، dict فيه {"total_balance": int, "participant_count": int} لعرضها باللوحة الجانبية.
    """
    card_width = 1360
    header_height = 108
    row_h = 74
    row_gap = 10
    pad = 30
    left_w = 800

    num_rows = max(len(top_users_data), 1)
    rows_area_h = num_rows * (row_h + row_gap) - row_gap
    total_height = max(header_height + rows_area_h + 40, 820)

    image = Image.new("RGBA", (card_width, total_height), (15, 16, 18, 255))
    draw = ImageDraw.Draw(image)

    font_title = get_arabic_font(28)
    font_sub = get_arabic_font(15)
    font_sec_title = get_arabic_font(18)
    font_name = get_arabic_font(23)
    font_rank = get_arabic_font(20)
    font_lvl = get_arabic_font(13)
    font_stat = get_arabic_font(19)
    font_stat_label = get_arabic_font(12)
    font_guide = get_arabic_font(13)
    font_guide_val = get_arabic_font(13)

    # ---- الهيدر ----
    draw.rectangle([0, 0, card_width, header_height], fill=(20, 21, 24, 255))
    draw.line([(0, header_height), (card_width, header_height)], fill=(35, 37, 42), width=2)

    if mode == "weekly":
        title_text = reshape_arabic("🏆 لوحة متصدري الأسبوع")
        sub_text = reshape_arabic(f"{guild_name} • يُحتسب XP هذا الأسبوع فقط")
    elif mode == "money":
        title_text = reshape_arabic(f"💰 أغنى أعضاء {guild_name}")
        sub_text = reshape_arabic(f"{guild_name} • أعلى 10 أرصدة بالبنك")
    elif mode == "teams":
        title_text = reshape_arabic("⚔️ لوحة حرب البيوت الملكية")
        sub_text = reshape_arabic(f"{guild_name} • ترتيب البيوت حسب نقاط الأسبوع")
    else:
        title_text = f"LEADERBOARD & XP REQUIREMENTS"
        sub_text = guild_name.upper()

    draw.text((card_width // 2, 42), title_text, fill=(255, 240, 205), font=font_title, anchor="mm")
    draw.text((card_width // 2, 74), sub_text, fill=(150, 155, 165), font=font_sub, anchor="mm")

    start_y = header_height + 20
    left_x1 = pad
    left_x2 = left_w
    # وضع "teams": تفاصيل كل بيت (اسم القائد + أسماء الأعضاء) تُمرَّر عبر money_stats["teams"] بنفس ترتيب الصفوف
    teams_extra = (money_stats or {}).get("teams", []) if mode == "teams" else []

    for idx, (rank_num, username, lvl, xp, needed_xp, avatar_bytes, weekly_xp) in enumerate(top_users_data):
        y = start_y + idx * (row_h + row_gap)
        tier = RANK_TIERS.get(rank_num, DEFAULT_TIER)

        # خلفية الصف (تناوب خفيف بين الصفوف العادية لتسهيل القراءة)
        if rank_num <= 3:
            bg = (26, 24, 20, 255)
        else:
            bg = (21, 22, 25, 255) if rank_num % 2 == 0 else (24, 26, 30, 255)
        draw.rounded_rectangle([left_x1, y, left_x2, y + row_h], radius=14, fill=bg)

        # خط تمييز جانبي ملوّن حسب فئة الترتيب
        draw.rounded_rectangle([left_x1, y + 6, left_x1 + 5, y + row_h - 6], radius=3, fill=tier["accent"])

        # توهج خفيف لأول 3 مراكز
        if tier["glow"]:
            overlay = Image.new("RGBA", (left_x2 - left_x1, row_h), (0, 0, 0, 0))
            odraw = ImageDraw.Draw(overlay)
            odraw.rounded_rectangle([0, 0, left_x2 - left_x1, row_h], radius=14, fill=tier["glow"])
            image.alpha_composite(overlay, (left_x1, y))
            draw = ImageDraw.Draw(image)

        # شارة الترتيب (دائرة)
        badge_cx, badge_cy, badge_r = left_x1 + 46, y + row_h // 2, 21
        if rank_num <= 3:
            draw.ellipse([badge_cx - badge_r, badge_cy - badge_r, badge_cx + badge_r, badge_cy + badge_r], fill=tier["accent"])
            draw.text((badge_cx, badge_cy), f"{rank_num}", fill=(20, 20, 22), font=font_rank, anchor="mm")
        else:
            draw.ellipse([badge_cx - badge_r, badge_cy - badge_r, badge_cx + badge_r, badge_cy + badge_r], outline=(70, 74, 84), width=2)
            draw.text((badge_cx, badge_cy), f"{rank_num}", fill=(190, 194, 204), font=font_rank, anchor="mm")

        # الصورة الرمزية (avatar) — نفس صف الشخص بالضبط، بحلقة ملوّنة حسب فئته
        av_size = 50
        av_x, av_y = badge_cx + badge_r + 14, y + (row_h - av_size) // 2
        ring_color = tier["accent"] if rank_num <= 3 else (60, 64, 74)
        draw.ellipse([av_x - 3, av_y - 3, av_x + av_size + 3, av_y + av_size + 3], outline=ring_color, width=3)
        if avatar_bytes:
            try:
                av_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((av_size, av_size), Image.Resampling.LANCZOS)
                mask = Image.new("L", (av_size, av_size), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, av_size, av_size), fill=255)
                av_img = ImageOps.fit(av_img, mask.size, centering=(0.5, 0.5))
                av_img.putalpha(mask)
                image.paste(av_img, (av_x, av_y), av_img)
                draw = ImageDraw.Draw(image)
            except Exception:
                pass

        # الاسم
        name_x = av_x + av_size + 18
        display_name = reshape_arabic(username[:20])
        draw.text((name_x, y + row_h // 2 - 2), display_name, fill=(240, 242, 246), font=font_name, anchor="lm")

        # شارة المستوى تحت الاسم (غير معروضة بوضع أغنى الأعضاء لأنها مو ذات صلة بالرصيد)
        if mode == "teams":
            t_ex = teams_extra[idx] if idx < len(teams_extra) else {}
            pill_txt = reshape_arabic(f"القائد: {str(t_ex.get('leader', ''))[:14]}")
            pw = draw.textlength(pill_txt, font=font_lvl)
            pill_x1, pill_y1 = name_x, y + row_h // 2 + 14
            pill_x2, pill_y2 = pill_x1 + pw + 16, pill_y1 + 18
            draw.rounded_rectangle([pill_x1, pill_y1, pill_x2, pill_y2], radius=9, fill=(38, 40, 46))
            draw.text(((pill_x1 + pill_x2) / 2, (pill_y1 + pill_y2) / 2), pill_txt, fill=(200, 204, 214), font=font_lvl, anchor="mm")
        elif mode != "money":
            pill_txt = f"LVL {lvl}"
            pw = draw.textlength(pill_txt, font=font_lvl)
            pill_x1, pill_y1 = name_x, y + row_h // 2 + 14
            pill_x2, pill_y2 = pill_x1 + pw + 16, pill_y1 + 18
            draw.rounded_rectangle([pill_x1, pill_y1, pill_x2, pill_y2], radius=9, fill=(38, 40, 46))
            draw.text(((pill_x1 + pill_x2) / 2, (pill_y1 + pill_y2) / 2), pill_txt, fill=(200, 204, 214), font=font_lvl, anchor="mm")

        # القيمة على اليمين: XP الأسبوع، إجمالي الـ XP، أو الرصيد حسب الوضع
        if mode == "weekly":
            stat_val = f"+{weekly_xp:,}"
            stat_label = reshape_arabic("XP هذا الأسبوع")
        elif mode == "teams":
            stat_val = f"{weekly_xp:,}"  # في وضع teams تُمرَّر نقاط البيت بنفس هذا الحقل
            stat_label = reshape_arabic("نقاط البيت")
        elif mode == "money":
            stat_val = f"{weekly_xp:,}"  # في وضع money يُمرَّر الرصيد بنفس هذا الحقل
            stat_label = reshape_arabic(f"{CURRENCY_NAME}")
        else:
            stat_val = f"{xp:,}"
            stat_label = "Total XP"
        stat_color = tier["accent"] if rank_num <= 3 else (235, 238, 245)
        draw.text((left_x2 - 22, y + row_h // 2 - 10), stat_val, fill=stat_color, font=font_stat, anchor="rm")
        draw.text((left_x2 - 22, y + row_h // 2 + 14), stat_label, fill=(130, 134, 145), font=font_stat_label, anchor="rm")

    # ---- اللوحة الجانبية: متطلبات XP لكل المستويات ----
    guide_x1 = left_x2 + 24
    guide_x2 = card_width - pad
    guide_y1 = start_y
    guide_y2 = total_height - 24

    draw.rounded_rectangle([guide_x1, guide_y1, guide_x2, guide_y2], radius=16, fill=(20, 21, 24, 255), outline=(38, 40, 46), width=1)

    if mode == "teams":
        panel_cx = (guide_x1 + guide_x2) // 2
        draw.text((panel_cx, guide_y1 + 26), reshape_arabic("أعضاء البيوت"), fill=(255, 240, 205), font=font_sec_title, anchor="mm")
        draw.line([(guide_x1 + 16, guide_y1 + 46), (guide_x2 - 16, guide_y1 + 46)], fill=(38, 40, 46), width=1)
        block_top = guide_y1 + 66
        block_h = min(150, (guide_y2 - block_top - 10) / max(len(teams_extra), 1))
        for t_i, t_ex in enumerate(teams_extra):
            by = block_top + t_i * block_h
            accent = RANK_TIERS.get(t_i + 1, DEFAULT_TIER)["accent"]
            draw.text((guide_x2 - 22, by), reshape_arabic(f"{t_i + 1}. {str(t_ex.get('name', ''))[:18]}"), fill=accent, font=font_stat_label, anchor="rm")
            for m_i, mname in enumerate(t_ex.get("members", [])):
                draw.text((guide_x2 - 34, by + 26 + m_i * 22), reshape_arabic(str(mname)[:16]), fill=(200, 204, 214), font=font_guide_val, anchor="rm")
    elif mode == "money":
        stats = money_stats or {}
        total_balance = stats.get("total_balance", 0)
        participant_count = stats.get("participant_count", 0)

        draw.text((guide_x1 + (guide_x2 - guide_x1) // 2, guide_y1 + 26), reshape_arabic("اقتصاد السيرفر"), fill=(255, 240, 205), font=font_sec_title, anchor="mm")
        draw.line([(guide_x1 + 16, guide_y1 + 46), (guide_x2 - 16, guide_y1 + 46)], fill=(38, 40, 46), width=1)

        panel_cx = (guide_x1 + guide_x2) // 2
        box_w = (guide_x2 - guide_x1) - 32
        box_h = 92
        stat1_y = guide_y1 + 90
        stat2_y = stat1_y + box_h + 22

        draw.rounded_rectangle([guide_x1 + 16, stat1_y, guide_x2 - 16, stat1_y + box_h], radius=14, fill=(26, 24, 20, 255), outline=(90, 74, 30), width=1)
        draw.text((panel_cx, stat1_y + 30), reshape_arabic("إجمالي الأرصدة"), fill=(190, 194, 204), font=font_guide, anchor="mm")
        draw.text((panel_cx, stat1_y + 62), f"{total_balance:,} {CURRENCY_NAME}", fill=(255, 240, 205), font=font_stat, anchor="mm")

        draw.rounded_rectangle([guide_x1 + 16, stat2_y, guide_x2 - 16, stat2_y + box_h], radius=14, fill=(21, 22, 25, 255), outline=(38, 40, 46), width=1)
        draw.text((panel_cx, stat2_y + 30), reshape_arabic("عدد الأعضاء الملاك"), fill=(190, 194, 204), font=font_guide, anchor="mm")
        draw.text((panel_cx, stat2_y + 62), f"{participant_count:,}", fill=(235, 238, 245), font=font_stat, anchor="mm")

        tip_y = stat2_y + box_h + 40
        if tip_y < guide_y2 - 20:
            draw.text((panel_cx, tip_y), reshape_arabic("استخدم /balance عشان تشوف رصيدك"), fill=(130, 134, 145), font=font_stat_label, anchor="mm")
    else:
        draw.text((guide_x1 + (guide_x2 - guide_x1) // 2, guide_y1 + 26), "ALL LEVELS XP (1 – 50)", fill=(255, 240, 205), font=font_sec_title, anchor="mm")
        draw.line([(guide_x1 + 16, guide_y1 + 46), (guide_x2 - 16, guide_y1 + 46)], fill=(38, 40, 46), width=1)

        mid_x = (guide_x1 + guide_x2) // 2
        draw.line([(mid_x, guide_y1 + 54), (mid_x, guide_y2 - 12)], fill=(32, 34, 39), width=1)

        content_top = guide_y1 + 58
        row_h2 = (guide_y2 - content_top - 8) / 25

        for lvl in range(1, 51):
            needed_val = get_xp_needed(lvl)
            needed_str = f"{needed_val:,}" if lvl < MAX_LEVEL else "MAX"

            if lvl <= 25:
                row_idx = lvl - 1
                col_x1 = guide_x1 + 16
                col_x2 = mid_x - 14
            else:
                row_idx = lvl - 26
                col_x1 = mid_x + 14
                col_x2 = guide_x2 - 16

            cur_y = content_top + (row_idx * row_h2) + (row_h2 / 2)
            is_special = lvl in (1, 11, 25, 50)
            label_color = (255, 240, 205) if is_special else (195, 198, 206)
            val_color = (255, 240, 205) if is_special else (140, 144, 154)

            draw.text((col_x1, cur_y), f"Lvl {lvl}", fill=label_color, font=font_guide, anchor="lm")
            draw.text((col_x2, cur_y), needed_str, fill=val_color, font=font_guide_val, anchor="rm")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

async def _build_leaderboard_image(guild: discord.Guild, mode: str = "alltime") -> io.BytesIO:
    """
    mode="alltime": يرتّب حسب المستوى ثم إجمالي الـ XP (الترتيب العام التاريخي).
    mode="weekly": يرتّب حسب XP المكتسب هذا الأسبوع فقط (weekly_xp)، ويستبعد من لم يكتسب أي XP هذا الأسبوع.
    """
    if mode == "weekly":
        with db_lock:
            cursor.execute("""
                SELECT user_id, level, xp, weekly_xp FROM users
                WHERE guild_id = ? AND weekly_xp > 0
                ORDER BY weekly_xp DESC
                LIMIT 10
            """, (guild.id,))
            top_users = cursor.fetchall()
    else:
        with db_lock:
            cursor.execute("""
                SELECT user_id, level, xp, weekly_xp FROM users 
                WHERE guild_id = ? 
                ORDER BY level DESC, xp DESC 
                LIMIT 10
            """, (guild.id,))
            top_users = cursor.fetchall()

    if not top_users:
        return None

    top_users_data = []
    for idx, (uid, lvl, xp, weekly_xp) in enumerate(top_users):
        member = guild.get_member(uid)
        username = member.display_name if member else f"User {uid}"
        needed = get_xp_needed(lvl)
        av_bytes = None
        if member:
            try:
                av_bytes = await member.display_avatar.with_format("png").read()
            except Exception:
                pass
        top_users_data.append((idx + 1, username, lvl, xp, needed, av_bytes, weekly_xp or 0))

    return await asyncio.to_thread(render_leaderboard_card_sync, guild.name, top_users_data, mode)

async def generate_leaderboard_image(guild: discord.Guild) -> io.BytesIO:
    """الليدربورد العام (كل الأوقات) — يرتّب حسب المستوى ثم إجمالي الـ XP. تستخدمه /leaderboard."""
    return await _build_leaderboard_image(guild, mode="alltime")

async def generate_weekly_leaderboard_image(guild: discord.Guild) -> io.BytesIO:
    """الليدربورد الأسبوعي — يرتّب حسب XP المكتسب هذا الأسبوع فقط. يستخدمه النشر الأسبوعي التلقائي وأمر /post_weekly_leaderboard."""
    return await _build_leaderboard_image(guild, mode="weekly")

async def generate_money_leaderboard_image(guild: discord.Guild) -> io.BytesIO:
    """ليدربورد أغنى الأعضاء — يرتّب حسب رصيد البنك (wallets.balance)، بنفس شكل الليدربورد الأسبوعي.
    يستخدمه النشر التلقائي كل 3 أيام وأمر /post_money_leaderboard."""
    with db_lock:
        cursor.execute("""
            SELECT user_id, balance FROM wallets
            WHERE guild_id = ? AND balance > 0
            ORDER BY balance DESC
            LIMIT 10
        """, (guild.id,))
        top_wallets = cursor.fetchall()

        cursor.execute("SELECT COALESCE(SUM(balance), 0), COUNT(*) FROM wallets WHERE guild_id = ? AND balance > 0", (guild.id,))
        total_balance, participant_count = cursor.fetchone()

    if not top_wallets:
        return None

    top_users_data = []
    for idx, (uid, balance) in enumerate(top_wallets):
        member = guild.get_member(uid)
        username = member.display_name if member else f"User {uid}"
        av_bytes = None
        if member:
            try:
                av_bytes = await member.display_avatar.with_format("png").read()
            except Exception:
                pass
        # lvl و xp و needed_xp مو مستخدمين بوضع money (اللوحة تخفي شارة المستوى تلقائياً)، والرصيد يُمرَّر بحقل weekly_xp
        top_users_data.append((idx + 1, username, 0, 0, 0, av_bytes, balance))

    money_stats = {"total_balance": total_balance or 0, "participant_count": participant_count or 0}
    return await asyncio.to_thread(render_leaderboard_card_sync, guild.name, top_users_data, "money", money_stats)

# ----------------- رسم بطاقة الرانك RANK القديمة (كصورة) -----------------
def render_rank_card_sync(avatar_bytes: bytes, username: str, level: int, xp: int, needed_xp: int, rank_pos: int, daily_xp: int, total_xp: int) -> io.BytesIO:
    width, height = 1160, 200
    card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)

    def draw_rounded_rect(xy, radius, fill):
        draw.rounded_rectangle(xy, radius=radius, fill=fill)

    # لوحة الألوان: أبيض/أسود/رمادي بس (بدون أي لون ذهبي)
    COLOR_WHITE = (245, 245, 245)
    COLOR_LABEL = (140, 144, 150)
    COLOR_BG_BOX = (24, 25, 28, 255)
    COLOR_BG_INNER = (18, 19, 21, 255)

    draw_rounded_rect([10, 10, 640, 190], 18, COLOR_BG_BOX)
    draw_rounded_rect([655, 10, 890, 95], 18, COLOR_BG_BOX)
    draw_rounded_rect([670, 48, 875, 87], 10, COLOR_BG_INNER)
    draw_rounded_rect([655, 105, 890, 190], 18, COLOR_BG_BOX)
    draw_rounded_rect([670, 142, 875, 182], 10, COLOR_BG_INNER)
    draw_rounded_rect([905, 10, 1150, 190], 18, COLOR_BG_BOX)

    font_name = get_arabic_font(34)
    font_header = get_arabic_font(14)
    font_val = get_arabic_font(26)
    font_exp = get_arabic_font(18)

    def draw_stat(cx: int, label_y: int, value_y: int, label: str, value: str,
                  value_font=font_val, value_color=COLOR_WHITE):
        """يرسم تسمية فوق قيمتها بمحاذاة موحّدة وفراغ صغير بينهما."""
        draw.text((cx, label_y), label, fill=COLOR_LABEL, font=font_header, anchor="mm")
        draw.text((cx, value_y), value, fill=value_color, font=value_font, anchor="mm")

    avatar_size = 130
    avatar_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
    mask = Image.new("L", (avatar_size, avatar_size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((0, 0, avatar_size, avatar_size), fill=255)
    avatar_cropped = ImageOps.fit(avatar_img, mask.size, centering=(0.5, 0.5))
    avatar_cropped.putalpha(mask)
    card.paste(avatar_cropped, (35, 35), avatar_cropped)

    draw.text((190, 32), reshape_arabic(username[:16]), fill=COLOR_WHITE, font=font_name)

    # الصف السفلي بالبوكس الأيسر: رانك / دايلي إكس بي / دايلي كاب — تسمية وقيمتها قريبين من بعض
    draw_stat(245, 108, 142, "SERVER RANK", f"#{rank_pos}")
    draw_stat(380, 108, 142, "DAILY XP", f"{daily_xp}")
    draw_stat(515, 108, 142, "DAILY CAP", f"{VOICE_DAILY_CAP}")

    # بوكس المستوى
    draw_stat(772, 27, 62, "LEVEL", f"{level}")

    # بوكس الـ EXP
    needed_str = f"{needed_xp}" if level < MAX_LEVEL else "MAX"
    draw_stat(772, 121, 155, "EXP", f"{xp} / {needed_str}", value_font=font_exp, value_color=COLOR_WHITE)

    # بوكس التوتال XP
    draw_stat(1027, 40, 78, "TOTAL XP", f"{total_xp:,}")

    progress = 1.0 if level >= MAX_LEVEL or needed_xp == 0 else min(1.0, max(0.0, xp / needed_xp))
    bar_x1, bar_y, bar_x2 = 10, 186, 640
    progress_width = int((bar_x2 - bar_x1) * progress)

    draw.line([(bar_x1, bar_y), (bar_x2, bar_y)], fill=(35, 38, 42), width=5)
    if progress_width > 0:
        draw.line([(bar_x1, bar_y), (bar_x1 + progress_width, bar_y)], fill=COLOR_WHITE, width=5)

    buffer = io.BytesIO()
    card.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

async def create_rank_card(member: discord.Member, level: int, xp: int, needed_xp: int, rank_pos: int, daily_xp: int) -> io.BytesIO:
    avatar_bytes = await member.display_avatar.with_format("png").read()
    total_xp = get_cumulative_xp(level, xp)
    return await asyncio.to_thread(render_rank_card_sync, avatar_bytes, member.name, level, xp, needed_xp, rank_pos, daily_xp, total_xp)

# ----------------- بطاقة الترحيب -----------------
def render_welcome_sync(avatar_bytes: bytes, username: str, member_count: int) -> io.BytesIO:
    width, height = 1000, 500
    try:
        base = Image.open("welcome_bg.png").convert("RGBA")
        base = ImageOps.fit(base, (width, height), centering=(0.5, 0.5))
    except Exception:
        base = Image.new("RGBA", (width, height), color=(15, 15, 15, 255))

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 160))
    base = Image.alpha_composite(base, overlay)
    draw = ImageDraw.Draw(base)

    avatar_size = 150
    avatar_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
    mask = Image.new("L", (avatar_size, avatar_size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((0, 0, avatar_size, avatar_size), fill=255)
    circular_avatar = ImageOps.fit(avatar_img, mask.size, centering=(0.5, 0.5))
    circular_avatar.putalpha(mask)

    avatar_x = (width - avatar_size) // 2
    avatar_y = 145

    draw.ellipse((avatar_x - 5, avatar_y - 5, avatar_x + avatar_size + 5, avatar_y + avatar_size + 5), outline=(255, 255, 255, 255), width=4)
    base.paste(circular_avatar, (avatar_x, avatar_y), circular_avatar)

    font_large = get_arabic_font(38)
    font_sub = get_arabic_font(22)
    font_count = get_arabic_font(18)

    draw.text((width // 2, 330), reshape_arabic("أهلاً وسهلاً بك في السيرفر"), fill=(210, 210, 210), font=font_sub, anchor="mm")
    draw.text((width // 2, 385), reshape_arabic(username), fill=(255, 255, 255), font=font_large, anchor="mm")
    draw.text((width // 2, 445), reshape_arabic(f"أنت العضو رقم #{member_count}"), fill=(160, 160, 160), font=font_count, anchor="mm")

    buffer = io.BytesIO()
    base.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

async def create_welcome_image(member: discord.Member) -> io.BytesIO:
    avatar_bytes = await member.display_avatar.with_format("png").read()
    return await asyncio.to_thread(render_welcome_sync, avatar_bytes, member.display_name, member.guild.member_count)

def apply_branding(embed: discord.Embed, guild: discord.Guild, user: discord.abc.User = None):
    if user:
        embed.set_author(name=f"{user.display_name} ({user.name})", icon_url=user.display_avatar.url)
    server_name = guild.name if guild else "B4E"
    embed.set_footer(text=server_name, icon_url=ICON_GIF_URL)

def apply_log_branding(embed: discord.Embed, guild: discord.Guild, target: discord.abc.User = None):
    """براندنق موحّد واحترافي لرسائل روم السجلات: صورة العضو المتأثر بارزة + أيقونة السجلات بالفوتر + توقيت دقيق."""
    if target:
        embed.set_thumbnail(url=target.display_avatar.url)
    server_name = guild.name if guild else "B4E"
    embed.set_footer(text=f"{server_name} • السجلات", icon_url=LOG_ICON_URL)
    embed.timestamp = discord.utils.utcnow()

def describe_channel_settings(channel: discord.abc.GuildChannel) -> list:
    """يرجّع قائمة أسطر توضح إعدادات القناة حسب نوعها (توبيك، NSFW، سلومود، بتريت، حد الأعضاء... إلخ)."""
    lines = []
    if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        topic = getattr(channel, "topic", None)
        lines.append(f"📝 **الموضوع:** {topic[:200] if topic else 'بدون'}")
        lines.append(f"🔞 **NSFW:** {'مفعّل ✅' if channel.nsfw else 'معطّل ❌'}")
        slowmode = getattr(channel, "slowmode_delay", 0)
        lines.append(f"🐢 **Slowmode:** {f'{slowmode} ثانية' if slowmode else 'معطّل'}")
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        lines.append(f"🎚️ **البتريت:** `{channel.bitrate // 1000} kbps`")
        if isinstance(channel, discord.VoiceChannel):
            lines.append(f"👥 **الحد الأقصى للأعضاء:** {channel.user_limit if channel.user_limit else 'بلا حد'}")
        region = getattr(channel, "rtc_region", None)
        if region:
            lines.append(f"🌍 **منطقة الصوت:** `{region}`")
    return lines

def describe_channel_overwrites(channel: discord.abc.GuildChannel) -> str:
    """يلخّص الصلاحيات المخصصة (Permission Overwrites) المضبوطة على القناة بخلاف صلاحية @everyone."""
    entries = []
    try:
        for target, overwrite in channel.overwrites.items():
            if target.id == channel.guild.default_role.id:
                continue
            label = target.mention if hasattr(target, "mention") else f"`{target.name}`"
            allowed, denied = overwrite.pair()
            allowed_list = [p for p, v in allowed if v]
            denied_list = [p for p, v in denied if v]
            summary = f"✅ {len(allowed_list)}" if allowed_list else ""
            if denied_list:
                summary += f" | 🚫 {len(denied_list)}"
            entries.append(f"{label} ({summary.strip(' |')})" if summary else label)
    except Exception:
        pass
    if not entries:
        return "بدون صلاحيات مخصصة (تابعة للكاتيجوري/@everyone فقط)"
    text = "، ".join(entries[:10])
    if len(entries) > 10:
        text += f" **+{len(entries) - 10} أخرى**"
    return text

def create_levelup_embed(member: discord.Member, level: int, guild: discord.Guild, reward: int = 0) -> discord.Embed:
    embed = discord.Embed(
        title=f"{member.display_name} leveled up!",
        description=f"🎖️ تهانينا {member.mention} ! ارتفع مستواك • وصلت الآن إلى اللفل **{level}** 🚀 • استمر في التفاعل (كتابة وصوت) لتفتح رتب أعلى! ✨",
        color=discord.Color.from_rgb(241, 196, 15)
    )
    if reward > 0:
        embed.add_field(name=f"{CURRENCY_EMOJI} مكافأة الترقية", value=f"`+{reward:,} {CURRENCY_NAME}`", inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    server_name = guild.name if guild else "B4E"
    embed.set_footer(text=server_name, icon_url=ICON_GIF_URL)
    return embed

# ----------------- تحديث إحصائيات السيرفر الآمن ضد الـ Rate Limit -----------------
async def update_single_stat(guild: discord.Guild, channel_id: int, new_name: str):
    if not channel_id or not guild:
        return
    ch = guild.get_channel(channel_id)
    if not ch or ch.name == new_name:
        return
    now = time.monotonic()
    if now - channel_update_cooldowns.get(channel_id, -float("inf")) < 600:
        return
    # Reserve BEFORE awaiting: simultaneous voice events must not queue edits.
    channel_update_cooldowns[channel_id] = now
    if not ch.permissions_for(guild.me).manage_channels:
        return
    try:
        await ch.edit(name=new_name)
    except discord.HTTPException as exc:
        print(f"[Stats] Channel {channel_id}: HTTP {exc.status}; next attempt delayed.")

async def update_server_stats(guild: discord.Guild):
    if not guild:
        return

    members = guild.members
    total_members = guild.member_count or len(members)
    bots = len([m for m in members if m.bot])
    humans = total_members - bots
    online_count = len([m for m in members if not m.bot and m.status != discord.Status.offline])
    in_voice_count = sum(len(vc.members) for vc in guild.voice_channels)

    stats = [
        (STAT_ONLINE_ID, f"Online: {online_count}"),
        (STAT_ALL_MEMBERS_ID, f"All Members & Bots: {total_members}"),
        (STAT_MEMBERS_ID, f"Members: {humans}"),
        (STAT_BOTS_ID, f"Bots: {bots}"),
        (STAT_IN_VOICE_ID, f"In Voice: {in_voice_count}"),
    ]

    for channel_id, new_name in stats:
        await update_single_stat(guild, channel_id, new_name)
        await asyncio.sleep(1)

@tasks.loop(minutes=10)
async def stats_task():
    for guild in bot.guilds:
        await update_server_stats(guild)

@tasks.loop(minutes=1)
async def voice_xp_task():
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            is_exempt_mute_vc = vc.id in EXEMPT_MUTE_VOICE_CHANNELS
            active_members = [
                m for m in vc.members 
                if not m.bot 
                and not m.voice.deaf 
                and not m.voice.self_deaf 
                and (is_exempt_mute_vc or (not m.voice.mute and not m.voice.self_mute))
            ]
            # تم إزالة شرط "لازم عضوين أو أكثر" — الآن يُحتسب XP حتى لو العضو لحاله بأي روم صوتي
            if len(active_members) >= 1:
                for member in active_members:
                    xp_gain = 2
                    new_level, leveled_up, reached_cap, levels_gained = await add_user_xp(guild.id, member.id, xp_gain, is_voice=True, track_quests=True)
                    await bump_quest(guild, member, "daily_voice", 1)

                    if reached_cap:
                        try:
                            dm_embed = discord.Embed(
                                title="⚠️ وصل حد نقاط الخبرة اليومي (Daily XP Cap)",
                                description=f"مرحباً {member.mention} 👋\nلقد وصلت للحد الأعلى لنقاط الخبرة من الرومات الصوتية (**{VOICE_DAILY_CAP} XP**).\n\nسيتجدد الحد اليومي تلقائياً عند الساعة 12 منتصف الليل! يمكنك متابعة التفاعل الكتابي لجمع المزيد من النقاط ✨.",
                                color=discord.Color.gold()
                            )
                            apply_branding(dm_embed, guild, user=member)
                            await send_optional_member_dm(member, member.guild.id, 'activity', embed=dm_embed)
                        except discord.Forbidden:
                            pass
                        except Exception as e:
                            print(f"Error sending DM cap alert: {e}")

                    if leveled_up:
                        reward = levels_gained * LEVEL_UP_REWARD
                        if reward > 0:
                            await add_balance(guild.id, member.id, reward, reason='مكافأة ترقية المستوى')
                        level_channel = bot.get_channel(LEVEL_CHANNEL_ID)
                        if level_channel:
                            embed = create_levelup_embed(member, new_level, guild, reward=reward)
                            await level_channel.send(embed=embed)
                        await update_member_level_role(member, new_level)

@tasks.loop(minutes=1)
async def afk_check_task():
    """يفحص كل دقيقة الأعضاء اللي مسكرين مايكهم (self mute) بشكل متواصل بالفويس،
    ولو وصلت مدة السكوت لـ AFK_MUTE_THRESHOLD_MINUTES يسجلهم بروم السجلات (مرة وحدة بس لكل فترة سكوت)."""
    now = time.time()
    for guild in bot.guilds:
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if not log_channel:
            continue
        for vc in guild.voice_channels:
            if vc.id not in AFK_TRACKED_CHANNEL_IDS or vc.id in AFK_EXCLUDED_CHANNEL_IDS:
                continue
            for m in vc.members:
                if m.bot or not m.voice or not m.voice.self_mute or m.voice.self_stream:
                    continue
                start = mute_start_times.get(m.id)
                if not start:
                    continue
                elapsed_minutes = (now - start) / 60
                if elapsed_minutes >= AFK_MUTE_THRESHOLD_MINUTES and m.id not in afk_logged_members:
                    afk_logged_members.add(m.id)
                    embed = discord.Embed(
                        title="💤 عضو محتمل AFK (مايكه مسكر من فترة)",
                        description=f"مايك {m.mention} مسكر (self mute) من أكثر من **{AFK_MUTE_THRESHOLD_MINUTES}** دقيقة بدون توقف.",
                        color=discord.Color.dark_gold()
                    )
                    embed.add_field(name="🎙️ الروم", value=vc.name, inline=True)
                    apply_log_branding(embed, guild, target=m)
                    try:
                        await log_channel.send(embed=embed)
                    except Exception as e:
                        print(f"[AFK Check] خطأ أثناء إرسال إشعار AFK: {e}")

@tasks.loop(hours=24)
async def warning_expiry_task():
    """يحذف تلقائياً كل الإنذارات اللي عدت WARNING_EXPIRY_DAYS يوم، مرة كل 24 ساعة."""
    try:
        deleted = await delete_expired_warnings()
        if deleted:
            print(f"[Warning Expiry] تم حذف {deleted} إنذار منتهي (أكثر من {WARNING_EXPIRY_DAYS} يوم).")
    except Exception as e:
        print(f"[Warning Expiry] خطأ أثناء حذف الإنذارات المنتهية: {e}")

# ----------------- تذكير مواقيت الصلاة -----------------
PRAYER_TZ = ZoneInfo("Asia/Riyadh")

_prayer_times_today = {}    # مثال: {"Fajr": "04:32", "Dhuhr": "11:45", ...} بتوقيت الرياض
_prayer_times_date = None   # تاريخ آخر تحديث لمواقيت اليوم (نعيد الجلب تلقائياً عند تغيّر اليوم)
_prayer_sent_today = set()  # الصلوات اللي سبق إرسال تذكيرها اليوم (تفادي تكرار نفس الإشعار)

async def refresh_prayer_times():
    """يجلب مواقيت صلاة اليوم من خدمة Aladhan API ويحدّث الكاش المحلي مرة وحدة كل يوم."""
    global _prayer_times_today, _prayer_times_date, _prayer_sent_today
    today = datetime.datetime.now(PRAYER_TZ).date()
    if _prayer_times_date == today and _prayer_times_today:
        return  # عندنا مواقيت اليوم فعلاً، ما نحتاج نطلب من جديد
    url = (
        "https://api.aladhan.com/v1/timingsByCity"
        f"?city={PRAYER_CITY}&country={PRAYER_COUNTRY}&method={PRAYER_CALC_METHOD}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
        timings = data["data"]["timings"]
        _prayer_times_today = {name: timings[name][:5] for name in PRAYER_NAMES_AR}  # نأخذ "HH:MM" بس
        _prayer_times_date = today
        _prayer_sent_today = set()
        print(f"[Prayer] تم تحديث مواقيت الصلاة ليوم {today}: {_prayer_times_today}")
    except Exception as e:
        print(f"[Prayer] تعذّر جلب مواقيت الصلاة: {e}")

def _build_adhan_source(prayer_key: str):
    """يبني مصدر صوت الأذان (ملف محلي أو رابط). لازم يُبنى من جديد لكل روم لأن المصدر يُستهلك مرة وحدة."""
    path = ADHAN_FAJR_AUDIO_SOURCE if (prayer_key == "Fajr" and ADHAN_FAJR_AUDIO_SOURCE) else ADHAN_AUDIO_SOURCE
    if not path:
        return None

    is_url = path.startswith("http://") or path.startswith("https://")
    if not is_url and not os.path.isfile(path):
        print(f"[Adhan] ملف الأذان غير موجود بالمسار: {path}")
        return None

    try:
        # خيارات إعادة الاتصال ضرورية مع الروابط عشان ما ينقطع الصوت لو تعثّر الاتصال
        before = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5" if is_url else None
        audio = discord.FFmpegPCMAudio(path, before_options=before, options="-vn")
        return discord.PCMVolumeTransformer(audio, volume=ADHAN_VOLUME)
    except Exception as e:
        print(f"[Adhan] تعذّر تجهيز مصدر الصوت (تأكد إن ffmpeg مثبّت): {e}")
        return None


async def play_adhan_in_guild(guild: discord.Guild, prayer_key: str):
    """يمرّ على الرومات الصوتية اللي فيها أعضاء بهذا السيرفر ويشغّل الأذان بكل وحد منها بالتسلسل."""
    if not ADHAN_VOICE_ENABLED or not guild.me:
        return

    candidates = []
    for vc in guild.voice_channels:
        humans = sum(1 for m in vc.members if not m.bot)
        if humans < ADHAN_MIN_HUMANS:
            continue
        perms = vc.permissions_for(guild.me)
        if not (perms.connect and perms.speak):
            continue
        candidates.append((humans, vc))

    if not candidates:
        return

    # نبدأ بالروم الأكثر أعضاءً عشان لو انقطع الوقت يكون وصل لأكبر عدد
    candidates.sort(key=lambda item: item[0], reverse=True)
    channels = [vc for _, vc in candidates[:ADHAN_MAX_CHANNELS_PER_GUILD]]

    # لو البوت أصلاً متصل بروم صوتي لسبب ثاني، ما نفصله نهائياً بالنهاية
    was_connected_before = guild.voice_client is not None
    voice_client = guild.voice_client

    try:
        for channel in channels:
            try:
                if voice_client and voice_client.is_connected():
                    if voice_client.channel.id != channel.id:
                        await voice_client.move_to(channel)
                else:
                    voice_client = await channel.connect(timeout=15.0, reconnect=False)

                await asyncio.sleep(1)  # مهلة بسيطة لاستقرار الاتصال قبل التشغيل

                source = _build_adhan_source(prayer_key)
                if source is None:
                    return

                finished = asyncio.Event()

                def _after_play(error, ev=finished):
                    if error:
                        print(f"[Adhan] خطأ أثناء التشغيل: {error}")
                    bot.loop.call_soon_threadsafe(ev.set)

                if voice_client.is_playing():
                    voice_client.stop()
                voice_client.play(source, after=_after_play)

                try:
                    await asyncio.wait_for(finished.wait(), timeout=ADHAN_MAX_SECONDS)
                except asyncio.TimeoutError:
                    pass  # تجاوز الحد الأقصى: نوقف وننتقل للروم اللي بعده

                if voice_client.is_playing():
                    voice_client.stop()

            except Exception as e:
                print(f"[Adhan] تعذّر تشغيل الأذان بروم {channel.name}: {e}")
    finally:
        if voice_client and voice_client.is_connected() and not was_connected_before:
            try:
                await voice_client.disconnect(force=True)
            except Exception:
                pass


async def play_adhan_everywhere(prayer_key: str):
    """يشغّل الأذان بكل السيرفرات بالتوازي (كل سيرفر بالتسلسل داخلياً، لأن البوت يدخل روم واحد بالسيرفر)."""
    if not ADHAN_VOICE_ENABLED:
        return
    await asyncio.gather(
        *(play_adhan_in_guild(guild, prayer_key) for guild in bot.guilds),
        return_exceptions=True
    )


async def send_prayer_reminder(prayer_key: str):
    """يرسل تذكير الصلاة للروم النصي الرئيسي + شات أي روم صوتي فيه أعضاء حقيقيين متصلين حالياً."""
    prayer_name_ar = PRAYER_NAMES_AR[prayer_key]
    embed = discord.Embed(
        description=f"حان الآن موعد أذان **{prayer_name_ar}** 🤲\nنسأل الله أن يتقبل منا ومنكم.",
        color=discord.Color.from_rgb(0, 150, 90)
    )

    main_channel = bot.get_channel(PRAYER_REMINDER_CHANNEL_ID)

    # نشغّل الأذان بالخلفية عشان ما يعطّل إرسال الرسائل النصية (التشغيل ممكن ياخذ دقيقة أو أكثر)
    if ADHAN_VOICE_ENABLED:
        asyncio.create_task(play_adhan_everywhere(prayer_key))

    if main_channel:
        try:
            embed_main = embed.copy()
            apply_branding(embed_main, main_channel.guild)
            # منشن @everyone عشان يوصل الجميع إشعار بوقت الصلاة
            sent_main_msg = await main_channel.send(
                content=f"@everyone\n**حان وقت صلاة {prayer_name_ar}**",
                embed=embed_main,
                allowed_mentions=discord.AllowedMentions(everyone=True)
            )
            # نحذف رسالة تذكير الصلاة تلقائياً بعد 5 دقائق
            await sent_main_msg.delete(delay=300)
        except Exception as e:
            print(f"[Prayer] تعذّر إرسال التذكير للروم الرئيسي: {e}")

    for guild in bot.guilds:
        for vc in guild.voice_channels:
            if vc.id == PRAYER_REMINDER_CHANNEL_ID:
                continue  # تفادي تكرار الإرسال لو نفس الآيدي صدفة
            has_humans = any(not m.bot for m in vc.members)
            if not has_humans:
                continue
            try:
                embed_vc = embed.copy()
                apply_branding(embed_vc, guild)
                # منشن @everyone داخل شات الروم الصوتي عشان ينبّه اللي جالسين بالروم
                sent_msg = await vc.send(
                    content=f"@everyone\n**حان وقت صلاة {prayer_name_ar}**",
                    embed=embed_vc,
                    allowed_mentions=discord.AllowedMentions(everyone=True)
                )
                # نحذف الرسالة تلقائياً بعد 10 دقائق بما إن الروم فيه ناس ونبي نخلي شاتها نظيف
                await sent_msg.delete(delay=600)
            except Exception as e:
                print(f"[Prayer] تعذّر إرسال التذكير بروم {vc.name}: {e}")

@tasks.loop(minutes=1)
async def prayer_reminder_task():
    """يفحص كل دقيقة هل دخل وقت أي صلاة من الصلوات الخمس، ويرسل تذكيرها باسمها بالضبط.
    نقارن بالدقائق (>=) بدل المطابقة الحرفية (==) عشان لو تأخرت دورة الفحص شوي (تحميل على البوت مثلاً)
    ما تفوتنا الصلاة ولا يوصل تذكيرها متأخر، لكن بحد أقصى دقيقتين تأخير حتى ما نرسل تذكير صلاة فاتت من زمان
    (مثلاً بعد إعادة تشغيل البوت بمنتصف اليوم)."""
    await refresh_prayer_times()
    if not _prayer_times_today:
        return
    now_dt = datetime.datetime.now(PRAYER_TZ)
    now_minutes = now_dt.hour * 60 + now_dt.minute
    for prayer_key, prayer_time in _prayer_times_today.items():
        if prayer_key in _prayer_sent_today:
            continue
        h, m = map(int, prayer_time.split(":"))
        prayer_minutes = h * 60 + m
        if 0 <= now_minutes - prayer_minutes <= 2:
            _prayer_sent_today.add(prayer_key)
            await send_prayer_reminder(prayer_key)

# ----------------- أذكار الصباح / المساء + الصلاة على النبي + حديث اليوم -----------------
def build_adhkar_embed(title: str, items: list, color, guild: discord.Guild = None) -> discord.Embed:
    """يبني إمبيد أذكار مرتب: كل ذكر برقمه الخاص (بخط عريض) مفصول بخط رفيع عن اللي بعده،
    مع علامة اتجاه من اليمين لليسار (RLM) قبل كل رقم حتى لا ينعكس اتجاه السطر
    (المشكلة اللي تصير لما يبدأ سطر عربي برقم لاتيني، فديسكورد أحياناً يحسبه سطر LTR).
    يضيف صورة السيرفر الفعلية (أيقونته) كصورة مصغّرة أعلى الإمبيد لو متوفرة."""
    RLM = "\u200f"  # Right-to-Left Mark
    separator = f"\n{RLM}┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
    lines = [f"{RLM}**{i}.** {RLM}{text}" for i, text in enumerate(items, start=1)]
    description = separator.join(lines)

    embed = discord.Embed(title=title, description=description, color=color)
    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    return embed

@tasks.loop(time=MORNING_ADHKAR_TIME)
async def morning_adhkar_task():
    channel = bot.get_channel(PRAYER_REMINDER_CHANNEL_ID)
    if not channel:
        return
    embed = build_adhkar_embed("☀️ أذكار الصباح", MORNING_ADHKAR, discord.Color.from_rgb(255, 193, 7), guild=channel.guild)
    apply_branding(embed, channel.guild)
    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[Adhkar] تعذّر إرسال أذكار الصباح: {e}")

@tasks.loop(time=EVENING_ADHKAR_TIME)
async def evening_adhkar_task():
    channel = bot.get_channel(PRAYER_REMINDER_CHANNEL_ID)
    if not channel:
        return
    embed = build_adhkar_embed("🌙 أذكار المساء", EVENING_ADHKAR, discord.Color.from_rgb(88, 101, 242), guild=channel.guild)
    apply_branding(embed, channel.guild)
    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[Adhkar] تعذّر إرسال أذكار المساء: {e}")

_salawat_shuffled = []

@tasks.loop(hours=SALAWAT_INTERVAL_HOURS)
async def salawat_task():
    global _salawat_shuffled
    channel = bot.get_channel(PRAYER_REMINDER_CHANNEL_ID)
    if not channel:
        return
    if not _salawat_shuffled:
        _salawat_shuffled = SALAWAT_PHRASES.copy()
        random.shuffle(_salawat_shuffled)
    phrase = _salawat_shuffled.pop()
    embed = discord.Embed(
        title="🤍 تذكير بالصلاة على النبي ﷺ",
        description=phrase,
        color=discord.Color.from_rgb(0, 168, 252)
    )
    apply_branding(embed, channel.guild)
    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[Salawat] تعذّر إرسال التذكير: {e}")

_hadith_shuffled = []

@tasks.loop(time=HADITH_TIME)
async def hadith_task():
    global _hadith_shuffled
    channel = bot.get_channel(PRAYER_REMINDER_CHANNEL_ID)
    if not channel:
        return
    if not _hadith_shuffled:
        _hadith_shuffled = HADITHS.copy()
        random.shuffle(_hadith_shuffled)
    hadith_text, hadith_source = _hadith_shuffled.pop()
    embed = discord.Embed(
        title="📖 حديث اليوم",
        description=f"{hadith_text}\n\n*{hadith_source}*",
        color=discord.Color.from_rgb(46, 139, 87)
    )
    apply_branding(embed, channel.guild)
    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[Hadith] تعذّر إرسال حديث اليوم: {e}")

@tasks.loop(minutes=20)
async def check_free_games_task():
    print("[Free Games] بدء تشغيل الفحص...")
    if not GIVEAWAY_CHANNEL_ID:
        print("[Free Games] ⚠️ توقف: GIVEAWAY_CHANNEL_ID = 0 (مو محدد بالكود).")
        return
    channel = bot.get_channel(GIVEAWAY_CHANNEL_ID)
    if not channel:
        print(f"[Free Games] ⚠️ توقف: ما لقيت روم بالآيدي {GIVEAWAY_CHANNEL_ID} — تأكد إن الآيدي صحيح وإن البوت عضو بالسيرفر وشايف الروم.")
        return
    print(f"[Free Games] الروم موجود: #{channel.name} ({channel.id}) — جاري الاتصال بالـ API...")

    api_url = "https://www.gamerpower.com/api/giveaways?type=game"
    ALLOWED_STORES = ["steam", "epic games", "gog", "ubisoft", "origin", "ea", "playstation", "xbox"]

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as response:
                if response.status == 201:
                    # حسب توثيق GamerPower الرسمي: 201 تعني "لا يوجد عروض نشطة حالياً" — مو خطأ فعلي
                    print("[Free Games] الـ API رجّع 201 (لا يوجد عروض ألعاب نشطة حالياً حسب فلترة type=game).")
                    return
                if response.status != 200:
                    body_preview = (await response.text())[:300]
                    print(f"[Free Games] فشل الاتصال بالـ API — Status Code: {response.status} | المحتوى: {body_preview}")
                    return
                data = await response.json()

        # بعض إصدارات API الخاصة بـ GamerPower ترجع القائمة مباشرة كمصفوفة []
        # وبعضها يرجعها ملفوفة بمفتاح "giveaways": {...} — نتعامل مع الحالتين بدل ما نفترض شكل واحد بس
        if isinstance(data, dict):
            games = data.get("giveaways", [])
        elif isinstance(data, list):
            games = data
        else:
            print(f"[Free Games] شكل غير متوقع من الـ API: {type(data)} — راجع استجابة الموقع.")
            return

        print(f"[Free Games] تم فحص الألعاب المجانية بنجاح — العدد الكلي من الـ API: {len(games)}")
        if games:
            sample_titles = [g.get("title", "?") for g in games[:3]]
            print(f"[Free Games] عيّنة من العناوين المستلمة: {sample_titles}")

        new_posts = 0
        skipped_wrong_store = 0
        skipped_already_posted = 0
        skipped_blocked_site = 0
        # مواقع نستثنيها رغم إنها تمر بفلترة المنصة، لأنها تتطلب حساب/نقاط عشان تفك المفتاح
        # (زي Alienware Arena اللي يطلب جمع نقاط ARP قبل ما تقدر تكلم الـ GET KEY)
        BLOCKED_SITES = ["alienwarearena.com"]
        for game in games:
            game_id = str(game.get("id"))
            platforms = game.get("platforms", "").lower()
            if not any(store in platforms for store in ALLOWED_STORES):
                skipped_wrong_store += 1
                continue

            giveaway_url_check = (game.get("open_giveaway_url") or game.get("gamerpower_url") or "").lower()
            if any(site in giveaway_url_check for site in BLOCKED_SITES):
                skipped_blocked_site += 1
                continue

            if game_id in posted_giveaways:
                skipped_already_posted += 1
                continue

            posted_giveaways.add(game_id)
            save_posted_game(game_id)
            new_posts += 1

            title = game.get("title", "Free Game Giveaway")
            display_platforms = game.get("platforms", "PC")
            worth = game.get("worth", "N/A")
            end_date = game.get("end_date", "N/A")
            published_date = game.get("published_date", "N/A")
            # بعض الألعاب يرجع لها الـ API حقل "image" فاضي أو غير موجود أصلاً (زي حالة Deadshot)
            # فنرجع نجرب حقل "thumbnail" كبديل قبل ما نستسلم ونسيب الإيمبيد بدون صورة
            image_url = game.get("image") or game.get("thumbnail")
            giveaway_url = game.get("open_giveaway_url") or game.get("gamerpower_url")

            embed = discord.Embed(title=f"{title}", color=discord.Color.from_rgb(0, 168, 252))
            embed.add_field(name="🏪 Platform / Store:", value=f"`{display_platforms}`", inline=False)
            embed.add_field(name="💰 Price:", value=f"~~{worth}~~ ➔ **FREE (100% Off)**", inline=False)
            embed.add_field(name="📅 Availability:", value=f"`{published_date}` ➔ `{end_date}`", inline=False)

            if image_url:
                embed.set_image(url=image_url)
            else:
                print(f"[Free Games] ⚠️ اللعبة '{title}' ما فيها لا image ولا thumbnail من الـ API — راح تُنشر بدون صورة رئيسية.")

            server_name = channel.guild.name if channel.guild else "B4E"
            embed.set_footer(text=server_name, icon_url=ICON_GIF_URL)
            embed.set_thumbnail(url="https://i.imgur.com/KRElNgr.gif")

            view = ButtonLimitedView()
            view.add_item(discord.ui.Button(label="اضغط للحصول على اللعبة", url=giveaway_url, emoji="🎮"))
            await channel.send(embed=embed, view=view)
            print(f"[Free Games] تم نشر لعبة جديدة: {title}")
            await asyncio.sleep(2)

        if new_posts == 0:
            print(f"[Free Games] ما فيه ألعاب جديدة تستاهل النشر — تم تجاهل {skipped_wrong_store} (منصة غير مدعومة) و{skipped_blocked_site} (موقع مستثنى مثل Alienware Arena) و{skipped_already_posted} (منشورة سابقاً) من أصل {len(games)}.")
    except Exception as e:
        print(f"[Free Games] Error: {type(e).__name__}: {e}")

# ----------------- مهمة إرسال المتصدرين كل نهاية أسبوع -----------------
# بداية يوم الجمعة (12:00 منتصف الليل) بتوقيت السعودية
WEEKLY_POST_TIME = datetime.time(hour=0, minute=0, second=0, tzinfo=RIYADH_TZ)
# رتبة "متصدر الأسبوع" — تنعطى لصاحب المركز الأول بالليدربورد وتفضل معه لين حد يتخطاه بالأسبوع اللي بعده
WEEKLY_TOP_ROLE_ID = 1545963775876792330
# مكافأة مالية تُعطى تلقائياً لمن يحصل على رتبة متصدر الأسبوع الجديد (لا تتكرر لو نفس الشخص ثبّت الصدارة أسبوع إضافي)
WEEKLY_TOP_ROLE_REWARD = 50

def get_top_xp_user_id(guild_id: int):
    # متصدر الأسبوع يُحسب حسب XP هذا الأسبوع فقط (weekly_xp) وليس المستوى/الإجمالي التاريخي
    with db_lock:
        cursor.execute("""
            SELECT user_id FROM users
            WHERE guild_id = ? AND weekly_xp > 0
            ORDER BY weekly_xp DESC
            LIMIT 1
        """, (guild_id,))
        row = cursor.fetchone()
        return row[0] if row else None

def build_weekly_top_embed(role: discord.Role, is_new: bool) -> discord.Embed:
    """يبني إيمبيد إعلان متصدر الأسبوع (جديد أو مثبّت) — مستخدمة بالمهمة التلقائية وبأمر المعاينة /test_weekly_top."""
    if is_new:
        title = "👑 متصدر الأسبوع الجديد!"
        intro = f"{{mention}} تصدّرت لوحة الـ XP الأسبوعية وحصلت على رتبة {role.mention} 🏆"
        reward_line = f"وتم إضافة `{WEEKLY_TOP_ROLE_REWARD:,} {CURRENCY_NAME}` {CURRENCY_EMOJI} على رصيدك مكافأة للصدارة!"
        closing = "استمر بنفس النشاط عشان تحافظ عليها للأسبوع الجاي!"
    else:
        title = "👑 تثبيت الصدارة!"
        intro = f"{{mention}} حافظ على صدارته لأسبوع آخر، ورتبة {role.mention} باقية معك! 🔥"
        reward_line = f"وتم إضافة `{WEEKLY_TOP_ROLE_REWARD:,} {CURRENCY_NAME}` {CURRENCY_EMOJI} على رصيدك مكافأة لهذا الأسبوع!"
        closing = "ما قدر أحد يتخطاك لين الحين، استمر!"

    description = (
        f"{intro}\n"
        f"{reward_line}\n"
        f"{closing}\n\n"
        "**من أبرز مزايا رتبة المتصدر الأسبوعي:**\n"
        "🎙️ Priority Speaker بالفويس\n"
        "🐢 تجاوز السلومود بالشات\n"
        "🎨 إضافة وإدارة إيموجي وستيكرات السيرفر\n"
        "📅 إنشاء وتعديل الفعاليات (Events)"
    )
    return discord.Embed(title=title, description=description, color=discord.Color.from_rgb(212, 175, 55))

async def update_weekly_top_role(guild: discord.Guild):
    if not WEEKLY_TOP_ROLE_ID:
        return
    role = guild.get_role(WEEKLY_TOP_ROLE_ID)
    if not role:
        print(f"[Weekly Top Role] ⚠️ ما لقيت الرتبة بالآيدي {WEEKLY_TOP_ROLE_ID}")
        return

    top_user_id = await asyncio.to_thread(get_top_xp_user_id, guild.id)
    if not top_user_id:
        return

    top_member = guild.get_member(top_user_id)
    if not top_member:
        return

    announce_channel = bot.get_channel(ROYAL_ANNOUNCE_CHANNEL_ID) if ROYAL_ANNOUNCE_CHANNEL_ID else None

    # لو المتصدر نفسه من الأسبوع الماضي، خله على حاله بدون أي تعديل بالرتبة (ولا نشيل ونرجع نعطي نفس الشخص)
    # لكن يستاهل مكافأة الأسبوع الجديد برضو لأنه فاز باللقب من جديد
    if role in top_member.roles:
        if WEEKLY_TOP_ROLE_REWARD:
            await add_balance(guild.id, top_member.id, WEEKLY_TOP_ROLE_REWARD, reason='مكافأة متصدر الأسبوع')
        if announce_channel:
            embed = build_weekly_top_embed(role, is_new=False)
            embed.description = embed.description.replace("{mention}", top_member.mention)
            apply_branding(embed, guild, user=top_member)
            try:
                await announce_channel.send(content=top_member.mention, embed=embed)
            except Exception as e:
                print(f"[Weekly Top Role] تعذّر إرسال إعلان تثبيت الصدارة: {e}")
        return

    # نشيل الرتبة من أي شخص ثاني حاطها حالياً (المتصدر السابق اللي انغلب)
    for m in list(role.members):
        try:
            await m.remove_roles(role, reason="تغيّر متصدر الأسبوع بالـ XP")
        except Exception as e:
            print(f"[Weekly Top Role] تعذّر إزالة الرتبة من {m}: {e}")

    try:
        await top_member.add_roles(role, reason="تصدّر لوحة الـ XP الأسبوعية")
    except Exception as e:
        print(f"[Weekly Top Role] تعذّر إعطاء الرتبة لـ {top_member}: {e}")
        return

    if WEEKLY_TOP_ROLE_REWARD:
        await add_balance(guild.id, top_member.id, WEEKLY_TOP_ROLE_REWARD, reason='مكافأة متصدر الأسبوع')

    if announce_channel:
        embed = build_weekly_top_embed(role, is_new=True)
        embed.description = embed.description.replace("{mention}", top_member.mention)
        apply_branding(embed, guild, user=top_member)
        try:
            await announce_channel.send(content=top_member.mention, embed=embed)
        except Exception as e:
            print(f"[Weekly Top Role] تعذّر إرسال إعلان المتصدر الجديد: {e}")

# ----------------- حروب البيوت الملكية الأسبوعية -----------------
war_lock = asyncio.Lock()
WAR_MEDALS = ["🥇", "🥈", "🥉", "4️⃣"]

def war_house_label(t: dict) -> str:
    return f"{t['emoji']} {t['name']}"

def war_latest_boundary(now: datetime.datetime) -> datetime.datetime:
    """آخر جمعة الساعة 12:00 صباحاً بتوقيت الرياض (بداية أسبوع الحرب الحالي)."""
    days_since_friday = (now.weekday() - 4) % 7
    return (now - datetime.timedelta(days=days_since_friday)).replace(hour=0, minute=0, second=0, microsecond=0)

def war_prizes_text() -> str:
    parts = []
    for i, prize in enumerate(WAR_PRIZES):
        parts.append(f"{WAR_MEDALS[i]} `{prize:,} {CURRENCY_NAME}` {CURRENCY_EMOJI}")
    suffix = "لكل عضو بالبيت" if WAR_PRIZE_PER_MEMBER else "للبيت (تنقسم على أعضائه)"
    return " • ".join(parts) + f" — {suffix}"

async def war_log(guild: discord.Guild, embed: discord.Embed, target=None):
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return
    apply_log_branding(embed, guild, target=target)
    try:
        await log_channel.send(embed=embed)
    except Exception as e:
        print(f"[Royal War] تعذّر الكتابة بروم السجلات: {e}")

def war_member_name(guild: discord.Guild, user_id: int) -> str:
    m = guild.get_member(user_id)
    return m.display_name if m else f"User {user_id}"

def war_member_mention(guild: discord.Guild, user_id: int) -> str:
    m = guild.get_member(user_id)
    return m.mention if m else f"<@{user_id}>"

def war_clean_name(name: str) -> str:
    """ينظّف الاسم عشان ما يخرّب تنسيق الإمبد (ماركداون + منشنات)."""
    name = discord.utils.escape_markdown(discord.utils.escape_mentions(str(name))).strip()
    return name[:28] if name else "عضو"

async def war_display_name(guild: discord.Guild, user_id: int) -> str:
    """يرجّع اسم العضو كنص عادي. منشنات الإمبد ما تنحل عند كل الناس (تطلع كأرقام لو عميل ديسكورد ما يعرف العضو)،
    فنعرض الاسم نفسه بدل المنشن، ونجيبه من الديسكورد لو مو موجود بالكاش."""
    m = guild.get_member(user_id)
    if m:
        return war_clean_name(m.display_name)
    try:
        m = await guild.fetch_member(user_id)
        return war_clean_name(m.display_name)
    except Exception:
        pass
    try:
        u = bot.get_user(user_id) or await bot.fetch_user(user_id)
        return war_clean_name(u.display_name)
    except Exception:
        return "عضو غادر السيرفر"

async def war_names_list(guild: discord.Guild, user_ids: list) -> list:
    return [await war_display_name(guild, uid) for uid in user_ids]

def war_role_for_team(guild: discord.Guild, team_idx: int):
    if 1 <= team_idx <= len(WAR_TEAM_ROLE_IDS):
        return guild.get_role(WAR_TEAM_ROLE_IDS[team_idx - 1])
    return None

async def war_sync_roles(guild: discord.Guild, team_defs: list, reason: str = "حرب البيوت الملكية") -> list:
    """يوزّع رتبة كل بيت على أعضائه، ويسحب رتب الحرب من أي أحد مو من أعضاء البيت (تنظيف الجولة السابقة).
    لو team_defs فاضية = يسحب الرتب من الكل. يرجّع قائمة مشاكل (إن وجدت) للسجلات."""
    problems = []
    desired = {}
    for t in team_defs:
        desired[WAR_TEAM_ROLE_IDS[t["idx"] - 1]] = set(t["member_ids"])
    for role_id in WAR_TEAM_ROLE_IDS:
        role = guild.get_role(role_id)
        if not role:
            problems.append(f"الرتبة `{role_id}` غير موجودة")
            continue
        want = desired.get(role_id, set())
        for m in list(role.members):
            if m.id not in want:
                try:
                    await m.remove_roles(role, reason=reason)
                except Exception as e:
                    problems.append(f"تعذّر سحب {role.name} من {m.display_name}: {e}")
        for uid in want:
            m = guild.get_member(uid)
            if m and role not in m.roles:
                try:
                    await m.add_roles(role, reason=reason)
                except Exception as e:
                    problems.append(f"تعذّر إعطاء {role.name} لـ {m.display_name}: {e} (تأكد إن رتبة البوت أعلى من رتب البيوت وعنده Manage Roles)")
    return problems

def war_cancel_week_sync(guild_id: int, week_id: int, finished_at: str):
    with db_lock:
        cursor.execute("UPDATE war_weeks SET status = 'cancelled', finished_at = ? WHERE guild_id = ? AND week_id = ?", (finished_at, guild_id, week_id))
        conn.commit()

async def generate_teams_leaderboard_image(guild: discord.Guild, ranked: list):
    top_data, extras = [], []
    for i, t in enumerate(ranked):
        leader = guild.get_member(t["leader_id"])
        av_bytes = None
        if leader:
            try:
                av_bytes = await leader.display_avatar.with_format("png").read()
            except Exception:
                pass
        top_data.append((i + 1, t["name"], 0, 0, 0, av_bytes, t["score"]))
        extras.append({
            "name": t["name"],
            "leader": await war_display_name(guild, t["leader_id"]),
            "members": await war_names_list(guild, [m["id"] for m in t["members"]]),
        })
    return await asyncio.to_thread(render_leaderboard_card_sync, guild.name, top_data, "teams", {"teams": extras})

class WarNameModal(discord.ui.Modal, title="تسمية بيتك الملكي"):
    house_name = discord.ui.TextInput(
        label="اسم البيت",
        placeholder="مثال: بيت الصقر",
        min_length=2,
        max_length=WAR_NAME_MAX_LEN,
        required=True,
    )

    def __init__(self, week_id: int, team_idx: int):
        super().__init__(timeout=300)
        self.week_id = week_id
        self.team_idx = team_idx

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        new_name = " ".join(discord.utils.escape_mentions(str(self.house_name.value)).split())
        if len(new_name) < 2:
            await interaction.response.send_message("❌ الاسم قصير جداً.", ephemeral=True)
            return

        result = await asyncio.to_thread(war_set_team_name_sync, guild.id, self.week_id, self.team_idx, new_name)
        if result == "already":
            await interaction.response.send_message("⚠️ سبق وسمّيت بيتك، الاسم ما ينتغير خلال نفس الحرب.", ephemeral=True)
            return
        if result == "duplicate":
            await interaction.response.send_message("❌ فيه بيت ثاني بنفس الاسم هذا الأسبوع، اختر اسم مختلف.", ephemeral=True)
            return

        teams = await asyncio.to_thread(war_get_teams_sync, guild.id, self.week_id)
        team = next((t for t in teams if t["idx"] == self.team_idx), None)
        await interaction.response.send_message(f"✅ تم تسمية بيتك: **{new_name}**", ephemeral=True)

        channel = bot.get_channel(WAR_CHANNEL_ID)
        if channel and team:
            member_names = await war_names_list(guild, [m["id"] for m in team["members"]])
            house_color = WAR_HOUSE_COLORS[(self.team_idx - 1) % len(WAR_HOUSE_COLORS)]
            embed = discord.Embed(
                title=f"{war_house_label(team)}",
                description=(
                    f"أعلن القائد **{war_clean_name(interaction.user.display_name)}** اسم بيته الجديد.\n\n"
                    f"**الأعضاء**\n" + "\n".join(f"> {n}" for n in member_names)
                ),
                color=discord.Color.from_rgb(*house_color)
            )
            apply_branding(embed, guild, user=interaction.user)
            try:
                await channel.send(embed=embed)
            except Exception as e:
                print(f"[Royal War] تعذّر إعلان اسم البيت: {e}")

        log_embed = discord.Embed(title="🏷️ تسمية بيت ملكي", color=discord.Color.from_rgb(255, 215, 0))
        log_embed.add_field(name="👑 القائد", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="🏠 الاسم الجديد", value=war_house_label(team) if team else new_name, inline=True)
        log_embed.add_field(name="🔢 رقم الحرب", value=f"`#{self.week_id}`", inline=True)
        await war_log(guild, log_embed, target=interaction.user)

class WarNameView(ButtonLimitedView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="تسمية البيت (للقادة فقط)", emoji="🏷️", style=discord.ButtonStyle.primary, custom_id="war_name_house_btn")
    async def name_house_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        info = await asyncio.to_thread(war_get_leader_team_sync, interaction.guild.id, interaction.user.id)
        if not info:
            await interaction.response.send_message("❌ هذا الزر لقادة البيوت فقط، وبالحرب الحالية.", ephemeral=True)
            return
        week_id, team_idx, _name, named = info
        if named:
            await interaction.response.send_message("⚠️ سبق وسمّيت بيتك، الاسم ما ينتغير خلال نفس الحرب.", ephemeral=True)
            return
        await interaction.response.send_modal(WarNameModal(week_id, team_idx))

    @discord.ui.button(label="فريقي ومساهمات الأعضاء", emoji="⚔️", style=discord.ButtonStyle.secondary, custom_id="war_my_team_btn")
    async def team_contributions_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_personal_team(interaction)

def war_build_allowed_leader_teams(eligible, prev_benched):
    leaders = [member for member in eligible if member.id in WAR_ALLOWED_LEADER_IDS]
    team_count = min(WAR_MAX_TEAMS, len(eligible) // WAR_TEAM_SIZE, len(leaders))
    if team_count < 2:
        return None, [], (
            f"ما يكفي لبدء حرب الفرق: {len(eligible)} عضو مؤهل و{len(leaders)} قائد مؤهل من القائمة. "
            f"المطلوب على الأقل {WAR_TEAM_SIZE * 2} أعضاء وقائدان مختلفان."
        )
    # أولوية الجولة السابقة تبقى داخل فئة القادة وداخل بقية الأعضاء.
    def priority_pool(members):
        priority = [member for member in members if member.id in prev_benched]
        rest = [member for member in members if member.id not in prev_benched]
        random.shuffle(priority)
        random.shuffle(rest)
        return priority + rest
    chosen_leaders = priority_pool(leaders)[:team_count]
    chosen_ids = {member.id for member in chosen_leaders}
    pool = priority_pool([member for member in eligible if member.id not in chosen_ids])
    needed = team_count * (WAR_TEAM_SIZE - 1)
    selected, benched = pool[:needed], pool[needed:]
    random.shuffle(selected)
    random.shuffle(chosen_leaders)
    defaults = WAR_HOUSE_DEFAULTS[:]
    random.shuffle(defaults)
    teams = []
    for index, leader in enumerate(chosen_leaders):
        group = [leader] + selected[index * (WAR_TEAM_SIZE - 1):(index + 1) * (WAR_TEAM_SIZE - 1)]
        name, emoji = defaults[index % len(defaults)]
        teams.append({"idx": index + 1, "name": name, "emoji": emoji,
                      "leader_id": leader.id, "member_ids": [member.id for member in group]})
    return teams, benched, None

async def _start_royal_war_locked(guild: discord.Guild, started_by=None):
    """يوزّع المؤهلين عشوائياً على بيوت (3 أعضاء لكل بيت)، يحدد قائد عشوائي لكل بيت، ويعلن ويسجّل. يرجّع (نجح؟، رسالة)."""
    if await asyncio.to_thread(war_get_active_week_sync, guild.id):
        return False, "فيه حرب نشطة حالياً، أنهها أولاً بأمر /royal_war_end."

    if not guild.chunked:
        try:
            await guild.chunk()
        except Exception:
            pass

    eligible = [m for m in guild.members if not m.bot and m.id in WAR_ALLOWED_MEMBER_IDS]
    # اللي انحرموا الأسبوع الماضي (زيادة عن العدد) ياخذون أولوية بهالأسبوع عشان التوزيع يكون عادل
    benched_raw = await asyncio.to_thread(get_bot_state, f"war_benched_{guild.id}") or ""
    prev_benched = {int(x) for x in benched_raw.split(",") if x.strip().isdigit()}
    team_defs, benched, error = war_build_allowed_leader_teams(eligible, prev_benched)
    if error:
        log_embed = discord.Embed(title="⚔️ تعذّر بدء حرب البيوت", description=error, color=discord.Color.red())
        await war_log(guild, log_embed)
        return False, error

    now = datetime.datetime.now(RIYADH_TZ)
    week_id = await asyncio.to_thread(war_create_week_sync, guild.id, now.isoformat(), team_defs)
    await asyncio.to_thread(set_bot_state, f"war_benched_{guild.id}", ",".join(str(m.id) for m in benched))

    end_ts = int((war_latest_boundary(now) + datetime.timedelta(days=7)).timestamp())

    # رتب البيوت (سحب القديمة + إعطاء الجديدة)
    role_problems = await war_sync_roles(guild, team_defs)

    header = discord.Embed(
        title=f"حرب البيوت الملكية  •  الجولة #{week_id}",
        description=(
            "قُسّم الأعضاء على البيوت بالقرعة، ولكل بيت قائد اختير عشوائيًا من قائمة القادة المحددة.\n"
            "البيت الأعلى نقاطاً بنهاية الأسبوع ياخذ الجوائز."
        ),
        color=discord.Color.from_rgb(255, 215, 0)
    )
    header.add_field(
        name="احتساب النقاط",
        value=(
            "› كل XP تكسبه (كتابة، صوت، تسجيل يومي)\n"
            f"› كل لقطة (فيديو أو صورة) = `{WAR_CLIP_POINTS}` نقاط، بحد أقصى `{WAR_CLIP_DAILY_CAP}` لقطات باليوم"
        ),
        inline=False
    )
    header.add_field(name="الجوائز", value=war_prizes_text(), inline=False)
    header.add_field(name="نهاية الحرب", value=f"<t:{end_ts}:F>  •  <t:{end_ts}:R>", inline=False)
    header.add_field(name="لقادة البيوت", value="اضغطوا زر التسمية بالأسفل لاختيار اسم بيتكم (مرة واحدة فقط).", inline=False)

    embeds = [header]
    for t in team_defs:
        color = WAR_HOUSE_COLORS[(t["idx"] - 1) % len(WAR_HOUSE_COLORS)]
        ordered_ids = [t["leader_id"]] + [uid for uid in t["member_ids"] if uid != t["leader_id"]]
        names = await war_names_list(guild, ordered_ids)
        member_lines = [f"👑  **{names[0]}**  — القائد"] + [f"▫️  {n}" for n in names[1:]]
        role = war_role_for_team(guild, t["idx"])
        role_line = f"\n\n**الرتبة:** {role.mention}" if role else ""
        house_embed = discord.Embed(
            title=war_house_label(t),
            description="\n".join(member_lines) + role_line,
            color=discord.Color.from_rgb(*color)
        )
        embeds.append(house_embed)

    if benched:
        benched_names = await war_names_list(guild, [m.id for m in benched])
        embeds.append(discord.Embed(
            title="خارج التوزيع هذا الأسبوع",
            description="، ".join(benched_names) + "\n\nلهم أولوية بالدخول الأسبوع القادم.",
            color=discord.Color.from_rgb(88, 92, 99)
        ))
    apply_branding(embeds[-1], guild)

    channel = bot.get_channel(WAR_CHANNEL_ID)
    if channel:
        leaders_text = " ".join(war_member_mention(guild, t["leader_id"]) for t in team_defs)
        try:
            await channel.send(
                content=f"@everyone\n👑 **قادة البيوت:** {leaders_text}",
                embeds=embeds,
                view=WarNameView(),
                allowed_mentions=discord.AllowedMentions(everyone=True, users=True, roles=False)
            )
        except Exception as e:
            print(f"[Royal War] تعذّر إرسال إعلان توزيع البيوت: {e}")

    log_embed = discord.Embed(title=f"⚔️ بدء حرب البيوت #{week_id}", color=discord.Color.from_rgb(255, 215, 0))
    log_embed.add_field(name="👥 المؤهلين", value=f"`{len(eligible)}` عضو", inline=True)
    log_embed.add_field(name="🏠 عدد البيوت", value=f"`{team_count}`", inline=True)
    log_embed.add_field(name="🛠️ بدأها", value=started_by.mention if started_by else "تلقائي (جمعة 12 صباحاً)", inline=True)
    for t in team_defs:
        log_embed.add_field(
            name=war_house_label(t),
            value=(
                f"👑 {war_member_mention(guild, t['leader_id'])} (قائد عشوائي)\n"
                f"👥 {' • '.join(war_member_mention(guild, uid) for uid in t['member_ids'])}"
            ),
            inline=False
        )
    if benched:
        log_embed.add_field(name="⏸️ خارج التوزيع", value=" ".join(m.mention for m in benched)[:1024], inline=False)
    if role_problems:
        log_embed.add_field(name="⚠️ مشاكل برتب البيوت", value="\n".join(role_problems)[:1024], inline=False)
    await war_log(guild, log_embed, target=started_by)
    return True, f"تم بدء حرب البيوت #{week_id} بـ {team_count} بيوت."

async def _finish_royal_war_locked(guild: discord.Guild, ended_by=None):
    """ينهي الحرب النشطة: يحسب الترتيب، يوزع الجوائز، يعلن مع ليدربورد الفرق، ويسجّل كل شي. يرجّع (نجح؟، رسالة)."""
    active = await asyncio.to_thread(war_get_active_week_sync, guild.id)
    if not active:
        return False, "ما فيه حرب نشطة حالياً."
    week_id = active[0]

    teams = await asyncio.to_thread(war_get_teams_sync, guild.id, week_id)
    ranked = sorted(teams, key=lambda t: (-t["score"], t["idx"]))
    # نقفل الحرب أول شي عشان لو صار خطأ أثناء صرف الجوائز ما تنصرف مرتين
    await asyncio.to_thread(war_finish_week_sync, guild.id, week_id, datetime.datetime.now(RIYADH_TZ).isoformat())

    payouts = {}  # team_idx -> [سطور صرف الجوائز للسجل]
    for i, t in enumerate(ranked):
        payouts[t["idx"]] = []
        if i >= len(WAR_PRIZES) or t["score"] <= 0:
            continue
        per_member = WAR_PRIZES[i] if WAR_PRIZE_PER_MEMBER else WAR_PRIZES[i] // max(1, len(t["members"]))
        t["per_member_prize"] = per_member
        for mem in t["members"]:
            if not guild.get_member(mem["id"]):
                payouts[t["idx"]].append(f"<@{mem['id']}> ⚠️ غادر السيرفر، ما انصرفت له الجائزة")
                continue
            try:
                new_balance = await add_balance(guild.id, mem["id"], per_member, reason='جائزة حرب الفرق')
                payouts[t["idx"]].append(f"<@{mem['id']}> +`{per_member:,}` ← الرصيد `{new_balance:,}`")
            except Exception as e:
                payouts[t["idx"]].append(f"<@{mem['id']}> ❌ فشل صرف الجائزة: {e}")

    # سحب رتب البيوت من الكل لأن الحرب انتهت
    role_problems = await war_sync_roles(guild, [], reason="انتهاء حرب البيوت الملكية")

    total_score = sum(t["score"] for t in ranked)
    channel = bot.get_channel(WAR_CHANNEL_ID)
    if channel:
        if total_score > 0:
            lines = []
            for i, t in enumerate(ranked):
                prize_txt = f"\n   🎁 `{t['per_member_prize']:,} {CURRENCY_NAME}` {CURRENCY_EMOJI} لكل عضو" if t.get("per_member_prize") else ""
                leader_name = await war_display_name(guild, t["leader_id"])
                member_names = await war_names_list(guild, [m["id"] for m in t["members"]])
                lines.append(
                    f"{WAR_MEDALS[min(i, len(WAR_MEDALS) - 1)]} **{war_house_label(t)}** — `{t['score']:,}` نقطة{prize_txt}\n"
                    f"   👑 {leader_name} • 👥 {'، '.join(member_names)}"
                )
            desc = "\n\n".join(lines)
            winner = ranked[0]
            title = f"🏆 انتهت حرب البيوت #{week_id} — الفائز: {war_house_label(winner)}"
        else:
            desc = "ما أحد سجّل نقاط هذا الأسبوع، فما فيه جوائز. الأسبوع الجاي فرصة جديدة! 💪"
            title = f"🏁 انتهت حرب البيوت #{week_id}"
        embed = discord.Embed(title=title, description=desc, color=discord.Color.from_rgb(255, 215, 0))
        apply_branding(embed, guild)

        discord_file = None
        try:
            image_buffer = await generate_teams_leaderboard_image(guild, ranked)
            if image_buffer:
                discord_file = discord.File(fp=image_buffer, filename="royal_war_leaderboard.png")
                embed.set_image(url="attachment://royal_war_leaderboard.png")
        except Exception as e:
            print(f"[Royal War] تعذّر رسم ليدربورد الفرق: {e}")
            traceback.print_exc()

        try:
            kwargs = {"content": f"@everyone\n🏆 **نتائج حرب البيوت الملكية | {guild.name}** 🏆", "embed": embed,
                      "allowed_mentions": discord.AllowedMentions(everyone=True, users=True, roles=False)}
            if discord_file:
                kwargs["file"] = discord_file
            await channel.send(**kwargs)
        except Exception as e:
            print(f"[Royal War] تعذّر إرسال النتائج: {e}")

    log_embed = discord.Embed(title=f"🏆 نتيجة حرب البيوت #{week_id}", color=discord.Color.from_rgb(255, 215, 0))
    log_embed.add_field(name="🛠️ أنهاها", value=ended_by.mention if ended_by else "تلقائي (جمعة 12 صباحاً)", inline=False)
    for i, t in enumerate(ranked):
        breakdown = "\n".join(
            f"<@{m['id']}> — XP: `{m['xp']:,}` | لقطات: `{m['clip']:,}`" for m in t["members"]
        )
        pay = "\n".join(payouts.get(t["idx"], [])) or "لا جائزة"
        log_embed.add_field(
            name=f"{WAR_MEDALS[min(i, len(WAR_MEDALS) - 1)]} {war_house_label(t)} — {t['score']:,} نقطة",
            value=f"{breakdown}\n**الجوائز:**\n{pay}"[:1024],
            inline=False
        )
    if role_problems:
        log_embed.add_field(name="⚠️ مشاكل بسحب رتب البيوت", value="\n".join(role_problems)[:1024], inline=False)
    await war_log(guild, log_embed, target=ended_by)
    return True, f"تم إنهاء حرب البيوت #{week_id} وإعلان النتائج."

async def start_royal_war(guild: discord.Guild, started_by=None):
    async with war_lock:
        return await _start_royal_war_locked(guild, started_by)

async def finish_royal_war(guild: discord.Guild, ended_by=None):
    async with war_lock:
        return await _finish_royal_war_locked(guild, ended_by)

@tasks.loop(minutes=10)
async def royal_war_task():
    """تدوّر الحرب أسبوعياً: كل جمعة 12 صباحاً (توقيت الرياض) تنهي حرب الأسبوع وتعلن النتائج وتبدأ حرب جديدة بتوزيع جديد.
    نفحص كل 10 دقايق ونقارن بوقت بدء آخر حرب المحفوظ بالقاعدة، فلو البوت كان مطفي وقت الجمعة يعوّض أول ما يشتغل،
    وإعادة التشغيل ما تكرر الإعلان."""
    channel = bot.get_channel(WAR_CHANNEL_ID)
    if not channel:
        return
    guild = channel.guild
    try:
        now = datetime.datetime.now(RIYADH_TZ)
        boundary = war_latest_boundary(now)
        last_started = await asyncio.to_thread(war_get_last_started_sync, guild.id)
        if last_started is None:
            if not WAR_AUTO_START_FIRST:
                return
        else:
            ls = datetime.datetime.fromisoformat(last_started)
            if ls.tzinfo is None:
                ls = ls.replace(tzinfo=RIYADH_TZ)
            if ls >= boundary:
                return

        # محاولة واحدة لكل أسبوع (لو فشل البدء بسبب قلة المؤهلين ما نعيد ونزعج السجلات كل 10 دقايق)
        state_key = f"war_last_rotate_{guild.id}"
        if await asyncio.to_thread(get_bot_state, state_key) == boundary.isoformat():
            return
        await asyncio.to_thread(set_bot_state, state_key, boundary.isoformat())

        async with war_lock:
            await _finish_royal_war_locked(guild)
            await _start_royal_war_locked(guild)
    except Exception as e:
        print(f"[Royal War] خطأ بالمهمة الأسبوعية: {e}")
        traceback.print_exc()

@royal_war_task.before_loop
async def before_royal_war_task():
    await bot.wait_until_ready()

@tasks.loop(time=WEEKLY_POST_TIME)
async def weekly_leaderboard_task():
    now_riyadh = datetime.datetime.now(RIYADH_TZ)
    if now_riyadh.weekday() != 4:  # 4 = الجمعة
        return

    channel = bot.get_channel(WEEKLY_LEADERBOARD_CHANNEL_ID)
    if not channel:
        return

    guild = channel.guild
    image_buffer = await generate_weekly_leaderboard_image(guild)
    if not image_buffer:
        return

    await update_weekly_top_role(guild)

    discord_file = discord.File(fp=image_buffer, filename="weekly_leaderboard.png")
    msg_content = (
        f"@everyone\n"
        f"🏆 **لوحة المتصدرين الأسبوعية | {guild.name}** 🏆\n"
        f"تهانينا للمتصدرين هذا الأسبوع! استمروا في التفاعل والنشاط الصوتي والكتابي لتصدر القائمة القادمة 🌟🔥"
    )
    await channel.send(content=msg_content, file=discord_file)

    # نصفّر عداد XP الأسبوعي للجميع عشان الأسبوع الجاي يبدأ من الصفر
    await reset_weekly_xp(guild.id)

# ----------------- مهمة إرسال أغنى الأعضاء كل 3 أيام -----------------
@tasks.loop(hours=1)
async def money_leaderboard_task():
    """تنشر تلقائياً كل 72 ساعة (3 أيام) صورة بنفس شكل الليدربورد الأسبوعي، لكن مرتّبة حسب رصيد البنك.
    نفحص كل ساعة ونقارن بآخر وقت نشر محفوظ بالقاعدة، فإعادة تشغيل/تحديث البوت ما تسبب نشر جديد."""
    channel = bot.get_channel(MONEY_LEADERBOARD_CHANNEL_ID)
    if not channel:
        return

    now_ts = time.time()
    last_raw = await asyncio.to_thread(get_bot_state, "money_leaderboard_last_post")
    if last_raw is None:
        # أول تشغيل بعد هذا التعديل: نسجّل الوقت الحالي بدون نشر، والنشر الجاي بعد 3 أيام
        await asyncio.to_thread(set_bot_state, "money_leaderboard_last_post", str(now_ts))
        return
    try:
        last_ts = float(last_raw)
    except ValueError:
        last_ts = 0.0
    if now_ts - last_ts < 72 * 3600:
        return

    guild = channel.guild
    image_buffer = await generate_money_leaderboard_image(guild)
    if not image_buffer:
        return

    discord_file = discord.File(fp=image_buffer, filename="money_leaderboard.png")
    msg_content = (
        f"@everyone\n"
        f"💰 **قائمة أغنى الأعضاء | {guild.name}** 💰\n"
        f"هذي قائمة بأصحاب أعلى رصيد بالبنك حالياً، تُحدَّث كل 3 أيام تلقائياً!"
    )
    await channel.send(content=msg_content, file=discord_file, allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False))
    await asyncio.to_thread(set_bot_state, "money_leaderboard_last_post", str(time.time()))

@tasks.loop(time=WEEKLY_POST_TIME)
async def weekly_best_clip_task():
    """تحسم مسابقة أفضل لقطة أسبوعية كل يوم جمعة (نفس توقيت لوحة المتصدرين): تكافئ صاحب أكثر لقطة
    تصويتات بروم اللقطات، تعلن النتيجة بروم الإعلانات، وتسجّل كل شي بروم السجلات، ثم تصفّر المسابقة."""
    now_riyadh = datetime.datetime.now(RIYADH_TZ)
    if now_riyadh.weekday() != 4:  # 4 = الجمعة
        return

    clip_channel = bot.get_channel(BEST_CLIP_CHANNEL_ID)
    if not clip_channel or not clip_channel.guild:
        return
    guild = clip_channel.guild

    best = await get_weekly_best_clip(guild.id)
    log_channel = bot.get_channel(LOG_CHANNEL_ID)

    if best:
        vote_message_id, entry_message_id, channel_id, author_id, vote_count = best
        winner = guild.get_member(author_id)
        new_balance = await add_balance(guild.id, author_id, BEST_CLIP_WEEKLY_REWARD, reason='مكافأة أفضل لقطة')

        announce_channel = bot.get_channel(BEST_CLIP_ANNOUNCE_CHANNEL_ID)
        if announce_channel:
            winner_mention = winner.mention if winner else f"<@{author_id}>"
            jump_url = f"https://discord.com/channels/{guild.id}/{channel_id}/{entry_message_id}"
            embed = discord.Embed(
                title="🎬 أفضل لقطة الأسبوع!",
                description=(
                    f"مبروك {winner_mention}! لقطتك حصدت أكثر تصويتات هذا الأسبوع بـ **{vote_count}** صوت 🗳️\n\n"
                    f"[اضغط هنا لمشاهدة اللقطة]({jump_url})\n\n"
                    f"وتم إضافة `{BEST_CLIP_WEEKLY_REWARD:,} {CURRENCY_NAME}` {CURRENCY_EMOJI} على رصيدك مكافأة! 🎉"
                ),
                color=discord.Color.from_rgb(255, 215, 0)
            )
            apply_branding(embed, guild, user=winner)
            try:
                await announce_channel.send(content=winner_mention, embed=embed)
            except Exception as e:
                print(f"[Best Clip] تعذّر إرسال إعلان الفائز: {e}")

        if log_channel:
            log_embed = discord.Embed(title="🏆 نتيجة مسابقة أفضل لقطة الأسبوع", color=discord.Color.from_rgb(255, 215, 0))
            log_embed.add_field(name="👤 الفائز", value=(winner.mention if winner else f"<@{author_id}>"), inline=True)
            log_embed.add_field(name="🗳️ عدد الأصوات", value=str(vote_count), inline=True)
            log_embed.add_field(name="💰 المكافأة", value=f"`{BEST_CLIP_WEEKLY_REWARD:,} {CURRENCY_NAME}`", inline=True)
            log_embed.add_field(name="💼 الرصيد بعد المكافأة", value=f"`{new_balance:,} {CURRENCY_NAME}`", inline=True)
            log_embed.add_field(name="🔗 رابط اللقطة", value=f"https://discord.com/channels/{guild.id}/{channel_id}/{entry_message_id}", inline=False)
            apply_log_branding(log_embed, guild, target=winner)
            await log_channel.send(embed=log_embed)
    else:
        if log_channel:
            log_embed = discord.Embed(
                title="🏆 نتيجة مسابقة أفضل لقطة الأسبوع",
                description="ما فيه لقطة وصلت لتصويتين على الأقل هذا الأسبوع؛ ما فيه فائز ولا مكافأة.",
                color=discord.Color.greyple()
            )
            apply_log_branding(log_embed, guild)
            await log_channel.send(embed=log_embed)

    # نصفّر كل مشاركات وأصوات هذا الأسبوع عشان المسابقة تبدأ من جديد الأسبوع الجاي
    await clear_weekly_clips(guild.id)

# ----------------- مهمة تهنئة أعياد الميلاد اليومية -----------------
# تُفحص كل يوم الساعة 12:05 صباحاً بتوقيت السعودية

def build_control_panel_guide(guild):
    embed = discord.Embed(
        title='🎛️ تحتاج أي خدمة في السيرفر؟',
        description=(f'توجّه إلى <#{QUEST_START_CHANNEL_ID}> وتفاعل مع **الكنترول بنل**!\n\n'
                     '🧭 **مهامي:** مهامك اليومية والأسبوعية وتقدمك فيها.\n'
                     '💰 **كشف البنك والتحويل:** رصيدك ومعاملاتك وتحويل الريالات.\n'
                     '🪪 **بروفايلي ورتبتي:** مستواك وخبرتك ومعلوماتك.\n'
                     '🎮 **اختيار الرانك:** اختيار رتبة روكت ليق.\n'
                     '⚔️ **فريقي:** متابعة فريقك ومساهمتك.\n'
                     '🗳️ **إرسال اقتراح:** شارك فكرتك مع الإدارة.\n'
                     '🛠️ **الدعم الفني:** افتح تذكرة إذا احتجت مساعدة.\n\n'
                     'اضغط الزر المناسب في اللوحة، وبتظهر لك الخيارات والخطوات.'),
        color=discord.Color.blue()
    )
    embed.set_image(url=BANNER_IMAGE_URL)
    apply_branding(embed, guild)
    return embed


@tasks.loop(time=CONTROL_PANEL_GUIDE_TIME)
async def public_control_panel_guide_task():
    channel = bot.get_channel(QUEST_REMINDER_CHANNEL_ID)
    if not channel:
        return
    day = datetime.datetime.now(RIYADH_TZ).date().isoformat()
    key = f'public_control_panel_guide_day:{channel.guild.id}'
    if await asyncio.to_thread(get_bot_state, key) == day:
        return
    view = ButtonLimitedView()
    view.add_item(discord.ui.Button(label='افتح قناة البداية والكنترول بنل', emoji='🎛️',
        style=discord.ButtonStyle.link,
        url=f'https://discord.com/channels/{channel.guild.id}/{QUEST_START_CHANNEL_ID}'))
    try:
        await channel.send(embed=build_control_panel_guide(channel.guild), view=view,
                           allowed_mentions=discord.AllowedMentions.none())
        await asyncio.to_thread(set_bot_state, key, day)
    except discord.HTTPException as exc:
        print(f'[Control Panel Guide] {exc}')


@public_control_panel_guide_task.before_loop
async def before_public_control_panel_guide():
    await bot.wait_until_ready()


def build_public_quest_reminder(guild, weekly_notice=False):
    embed = discord.Embed(
        title='🧭 تذكير بالمهام اليومية والأسبوعية',
        description=(f'مرّ على <#{QUEST_START_CHANNEL_ID}> وافتح **«مهامي»** من لوحة التحكم.\n\n'
                     '☀️ **اليومية:** شوف مهامك وتقدمك وكم باقي لك قبل تجددها.\n'
                     '📅 **الأسبوعية:** راجع أهداف الأسبوع؛ بعض المهام تحتاج نشاطًا بأيام مختلفة.\n\n'
                     + ('⏳ **تذكير نهاية الأسبوع:** المهام الأسبوعية تتجدد يوم الجمعة؛ راجع اللي باقي عليك اليوم.\n\n'
                        if weekly_notice else '')
                     + 'كل عضو له مهامه الخاصة؛ ادخل وشوف وش عليك اليوم!'),
        color=discord.Color.blue()
    )
    embed.set_image(url=BANNER_IMAGE_URL)
    apply_branding(embed, guild)
    return embed


@tasks.loop(time=QUEST_REMINDER_TIME)
async def public_quest_reminder_task():
    channel = bot.get_channel(QUEST_REMINDER_CHANNEL_ID)
    if not channel:
        return
    now = datetime.datetime.now(RIYADH_TZ)
    key = f'public_quest_reminder_day:{channel.guild.id}'
    day = now.date().isoformat()
    if await asyncio.to_thread(get_bot_state, key) == day:
        return
    view = ButtonLimitedView()
    view.add_item(discord.ui.Button(label='روح لمهامك في قناة البداية', emoji='🧭',
        style=discord.ButtonStyle.link,
        url=f'https://discord.com/channels/{channel.guild.id}/{QUEST_START_CHANNEL_ID}'))
    try:
        await channel.send(embed=build_public_quest_reminder(channel.guild, now.weekday() == 3),
                           view=view, allowed_mentions=discord.AllowedMentions.none())
        await asyncio.to_thread(set_bot_state, key, day)
    except discord.HTTPException as exc:
        print(f'[Public Quest Reminder] {exc}')


@public_quest_reminder_task.before_loop
async def before_public_quest_reminder():
    await bot.wait_until_ready()


BIRTHDAY_CHECK_TIME = datetime.time(hour=0, minute=5, second=0, tzinfo=RIYADH_TZ)

@tasks.loop(time=BIRTHDAY_CHECK_TIME)
async def birthday_task():
    today_riyadh = datetime.datetime.now(RIYADH_TZ)
    today_str = today_riyadh.date().isoformat()

    for guild in bot.guilds:
        # إزالة رتبة المناسبات الخاصة ممن أخذوها قبل اليوم فقط
        # (لو عضو أخذها اليوم نفسه -مثلاً عن طريق /catch_up_anniversaries بالمساء- ما تنشال منه إلا بعد يوم كامل)
        role = guild.get_role(BIRTHDAY_ROLE_ID) if BIRTHDAY_ROLE_ID else None
        if role:
            for m in list(role.members):
                granted_date = get_special_role_grant_date(guild.id, m.id)
                if granted_date == today_str:
                    continue  # أخذها اليوم نفسه، لسا ما اكتمل يومها
                try:
                    await m.remove_roles(role, reason="انتهاء يوم المناسبة الخاصة")
                except Exception:
                    pass
                clear_special_role_grant(guild.id, m.id)

        channel = guild.get_channel(BIRTHDAY_CHANNEL_ID) if BIRTHDAY_CHANNEL_ID else None

        async def celebrate(member: discord.Member, embed: discord.Embed):
            if channel:
                try:
                    await channel.send(content=member.mention, embed=embed)
                except Exception as e:
                    print(f"[Birthdays] خطأ أثناء إرسال رسالة التهنئة: {e}")
            if role:
                try:
                    await member.add_roles(role, reason="مناسبة خاصة اليوم")
                    record_special_role_grant(guild.id, member.id, today_str)
                    if BIRTHDAY_ROLE_REWARD:
                        await add_balance(guild.id, member.id, BIRTHDAY_ROLE_REWARD, reason='مكافأة عيد الميلاد')
                except Exception:
                    pass

        # 1) أعياد الميلاد المسجّلة يدوياً عبر /set_birthday
        if channel:
            with db_lock:
                cursor.execute(
                    "SELECT user_id FROM birthdays WHERE guild_id = ? AND month = ? AND day = ?",
                    (guild.id, today_riyadh.month, today_riyadh.day)
                )
                birthday_user_ids = cursor.fetchall()
            for (uid,) in birthday_user_ids:
                if already_celebrated(guild.id, uid, today_riyadh.year, "birthday"):
                    continue
                member = guild.get_member(uid)
                if not member:
                    continue
                embed = discord.Embed(
                    title="🎂 عيد ميلاد سعيد!",
                    description=(
                        f"كل عام وأنت بخير يا {member.mention} 🎉\n"
                        "نتمنى لك سنة مليئة بالصحة والسعادة والنجاح، ويسعدنا وجودك بمجتمعنا. 🌟"
                    ),
                    color=discord.Color.from_rgb(255, 105, 180)
                )
                apply_branding(embed, guild, user=member)
                embed.set_thumbnail(url=member.display_avatar.url)
                embed.add_field(name="⏳ المدة", value=f"تم منحك رتبة <@&{BIRTHDAY_ROLE_ID}> لمدة يوم واحد 🎗️", inline=False)
                embed.add_field(name="🎁 مميزات الرول لليوم", value=(
                    "📌 تثبيت وفك تثبيت أي رسالة\n"
                    "🔇 كتم ونقل الأعضاء بالرومات الصوتية\n"
                    "✏️ تغيير نك نيم أي عضو\n"
                    "📢 مانشن @everyone / @here / كل الرولات\n"
                    "🎤 أولوية بالتحدث بالفويس (Priority Speaker)\n"
                    "⚡ تجاوز وضع الـ Slowmode"
                ), inline=False)
                await celebrate(member, embed)
                mark_celebrated(guild.id, uid, today_riyadh.year, "birthday")

        # 2) الذكرى السنوية للانضمام للسيرفر + عيد ميلاد إنشاء حساب الديسكورد (تلقائي بدون أي تسجيل)
        for member in guild.members:
            if member.bot:
                continue

            if member.joined_at:
                joined_riyadh = member.joined_at.astimezone(RIYADH_TZ)
                years = today_riyadh.year - joined_riyadh.year
                if (years >= 1 and joined_riyadh.month == today_riyadh.month and joined_riyadh.day == today_riyadh.day
                        and not already_celebrated(guild.id, member.id, today_riyadh.year, "join")):
                    embed = discord.Embed(
                        title="🎉 ذكرى انضمام سعيدة!",
                        description=(
                            f"مرّت **{years}** سنة كاملة على انضمام {member.mention} لعائلة **{guild.name}**! 🥳\n"
                            "شكراً لبقائك معنا طول هالمدة 💙"
                        ),
                        color=discord.Color.from_rgb(88, 101, 242)
                    )
                    apply_branding(embed, guild, user=member)
                    embed.set_thumbnail(url=member.display_avatar.url)
                    embed.add_field(name="⏳ المدة", value=f"تم منحك رتبة <@&{BIRTHDAY_ROLE_ID}> لمدة يوم واحد 🎗️", inline=False)
                    embed.add_field(name="🎁 مميزات الرول لليوم", value=(
                        "📌 تثبيت وفك تثبيت أي رسالة\n"
                        "🔇 كتم ونقل الأعضاء بالرومات الصوتية\n"
                        "✏️ تغيير نك نيم أي عضو\n"
                        "📢 مانشن @everyone / @here / كل الرولات\n"
                        "🎤 أولوية بالتحدث بالفويس (Priority Speaker)\n"
                        "⚡ تجاوز وضع الـ Slowmode"
                    ), inline=False)
                    await celebrate(member, embed)
                    mark_celebrated(guild.id, member.id, today_riyadh.year, "join")

            created_riyadh = member.created_at.astimezone(RIYADH_TZ)
            acc_years = today_riyadh.year - created_riyadh.year
            if (acc_years >= 1 and created_riyadh.month == today_riyadh.month and created_riyadh.day == today_riyadh.day
                    and not already_celebrated(guild.id, member.id, today_riyadh.year, "account")):
                embed = discord.Embed(
                    title="🎈 عيد ميلاد حسابك في ديسكورد!",
                    description=f"اليوم يصادف مرور **{acc_years}** سنة على إنشاء حساب {member.mention} في ديسكورد! 🎈",
                    color=discord.Color.from_rgb(114, 137, 218)
                )
                apply_branding(embed, guild, user=member)
                embed.set_thumbnail(url=member.display_avatar.url)
                embed.add_field(name="⏳ المدة", value=f"تم منحك رتبة <@&{BIRTHDAY_ROLE_ID}> لمدة يوم واحد 🎗️", inline=False)
                embed.add_field(name="🎁 مميزات الرول لليوم", value=(
                    "📌 تثبيت وفك تثبيت أي رسالة\n"
                    "🔇 كتم ونقل الأعضاء بالرومات الصوتية\n"
                    "✏️ تغيير نك نيم أي عضو\n"
                    "📢 مانشن @everyone / @here / كل الرولات\n"
                    "🎤 أولوية بالتحدث بالفويس (Priority Speaker)\n"
                    "⚡ تجاوز وضع الـ Slowmode"
                ), inline=False)
                await celebrate(member, embed)
                mark_celebrated(guild.id, member.id, today_riyadh.year, "account")

# ----------------- نظام النسخ الاحتياطي الدوري لقاعدة البيانات -----------------
# يرسل نسخة من ملف levels.db (يحتوي المستويات، الإنذارات، أعياد الميلاد، الدعوات...) إلى روم السجلات
# عشان لو صار كراش أو مشكلة استضافة، تقدر تسترجع كل البيانات بدل ما تخسرها بالكامل
BACKUP_TIME = datetime.time(hour=4, minute=0, second=0, tzinfo=RIYADH_TZ)
DB_FILE_PATH = "levels.db"

async def save_and_send_safety_backup(reason,requested_by=None):
    path=await asyncio.to_thread(create_safety_backup_sync,reason,backup_command_manifest())
    channel=bot.get_channel(LOG_CHANNEL_ID)
    if not channel:
        print(f'[Safety Backup] النسخة المحلية محفوظة: {path}؛ روم السجلات غير متاح.')
        return path,False
    try:
        embed=discord.Embed(title='🗄️ نسخة حفظ البوت',description=reason,color=discord.Color.teal())
        embed.add_field(name='المحتويات',value='السكربت + قاعدة البيانات + قائمة أوامر السلاش',inline=False)
        embed.add_field(name='الحجم',value=f'{os.path.getsize(path)/1024:.1f} KB',inline=True)
        apply_log_branding(embed,channel.guild,target=requested_by)
        await channel.send(embed=embed,file=discord.File(path),allowed_mentions=discord.AllowedMentions.none())
        return path,True
    except Exception as exc:
        print(f'[Safety Backup] النسخة المحلية محفوظة لكن رفعها فشل: {exc}')
        return path,False

async def send_db_backup(reason: str,requested_by: discord.abc.User=None):
    try:
        _,uploaded=await save_and_send_safety_backup(reason,requested_by)
        return uploaded
    except Exception as exc:
        print(f'[Safety Backup] فشل إنشاء النسخة: {exc}')
        return False

@tasks.loop(minutes=SAFETY_BACKUP_INTERVAL_MINUTES)
async def local_safety_backup_task():
    try:
        await asyncio.to_thread(create_safety_backup_sync,'نسخة محلية دورية',backup_command_manifest())
    except Exception as exc:
        print(f'[Safety Backup] فشل الحفظ الدوري: {exc}')

@local_safety_backup_task.before_loop
async def before_local_safety_backup_task():
    await bot.wait_until_ready()

@tasks.loop(time=BACKUP_TIME)
async def db_backup_task():
    await send_db_backup(reason="نسخة احتياطية تلقائية يومية (الساعة 4:00 صباحاً بتوقيت الرياض)")

# ----------------- مهمة الرواتب الشهرية التلقائية -----------------
# تُفحص كل يوم الساعة 23:50 بتوقيت الرياض، ولو اليوم آخر يوم بالشهر تُصرف الرواتب لأصحاب الرتب المؤهلة (SALARY_ROLES)
SALARY_CHECK_TIME = datetime.time(hour=23, minute=50, second=0, tzinfo=RIYADH_TZ)

async def pay_monthly_salaries(guild: discord.Guild) -> int:
    """يصرف الراتب الشهري لكل عضو عنده رتبة مؤهلة (تُجمع رواتب أكثر من رتبة)، يرسله له بالخاص،
    يعلن عنه برسالة وحدة بروم الإعلانات، ويرسل تقرير مفصّل بروم السجلات.
    يرجّع عدد الأعضاء اللي انصرف لهم راتب."""
    paid_members = []
    for member in guild.members:
        if member.bot:
            continue
        salary = calculate_member_salary(member)
        if salary <= 0:
            continue
        new_balance = await add_balance(guild.id, member.id, salary, reason='الراتب الشهري')
        paid_members.append((member, salary, new_balance))

        try:
            dm_embed = discord.Embed(
                title=f"{CURRENCY_EMOJI} نزل راتبك الشهري!",
                description=(
                    f"تم إضافة راتبك الشهري من سيرفر **{guild.name}**.\n\n"
                    f"➕ المبلغ المضاف: `{salary:,} {CURRENCY_NAME}`\n"
                    f"💼 رصيدك الحالي: `{new_balance:,} {CURRENCY_NAME}`"
                ),
                color=discord.Color.from_rgb(241, 196, 15)
            )
            apply_branding(dm_embed, guild, user=member)
            await send_optional_member_dm(member,guild.id,'financial',embed=dm_embed)
        except Exception:
            pass  # الخاص مقفل أو صار خطأ، نتجاهل ونكمل مع باقي الأعضاء

    if paid_members:
        announce_channel = bot.get_channel(SALARY_ANNOUNCE_CHANNEL_ID)
        if announce_channel:
            announce_embed = discord.Embed(
                title=f"{CURRENCY_EMOJI} صرف الرواتب الشهرية",
                description=f"تم صرف الراتب الشهري لكل الأعضاء المستحقين (**{len(paid_members)}** عضو). تفقّدوا الخاص! 📩",
                color=discord.Color.from_rgb(241, 196, 15)
            )
            apply_branding(announce_embed, guild)
            try:
                await announce_channel.send(embed=announce_embed)
            except Exception as e:
                print(f"[Salaries] خطأ أثناء إرسال إعلان الرواتب: {e}")

        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            total_paid = sum(s for _, s, _ in paid_members)
            lines = [f"{m.mention}: `+{s:,} {CURRENCY_NAME}` (الرصيد الآن `{b:,} {CURRENCY_NAME}`)" for m, s, b in paid_members[:25]]
            extra = f"\n...و{len(paid_members) - 25} عضو إضافي." if len(paid_members) > 25 else ""
            embed = discord.Embed(
                title=f"{CURRENCY_EMOJI} تقرير صرف الرواتب الشهرية",
                description=(
                    f"**عدد المستفيدين:** {len(paid_members)}\n"
                    f"**إجمالي المبلغ المصروف:** `{total_paid:,} {CURRENCY_NAME}`\n\n" + "\n".join(lines) + extra
                ),
                color=discord.Color.from_rgb(241, 196, 15)
            )
            apply_log_branding(embed, guild)
            try:
                await log_channel.send(embed=embed)
            except Exception as e:
                print(f"[Salaries] خطأ أثناء إرسال تقرير الرواتب: {e}")

    return len(paid_members)

@tasks.loop(time=SALARY_CHECK_TIME)
async def monthly_salary_task():
    today_riyadh = datetime.datetime.now(RIYADH_TZ)
    tomorrow_riyadh = today_riyadh + datetime.timedelta(days=1)
    if tomorrow_riyadh.month == today_riyadh.month:
        return  # لسا مو آخر يوم بالشهر

    for guild in bot.guilds:
        if was_salary_paid_this_month_sync(guild.id, today_riyadh.year, today_riyadh.month):
            continue
        await pay_monthly_salaries(guild)
        mark_salary_paid_sync(guild.id, today_riyadh.year, today_riyadh.month)

# 11. أمر تعويض المناسبات اللي فاتت هالسنة (يُشغّل يدوياً مرة عند التفعيل الأول أو عند الحاجة)
@bot.tree.command(name="catch_up_anniversaries", description="إرسال تهنئة فورية لكل عضو فاتت ذكرى انضمامه/حسابه هالسنة (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@is_owner_only()
async def catch_up_anniversaries(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    today_riyadh = datetime.datetime.now(RIYADH_TZ)
    today_tuple = (today_riyadh.month, today_riyadh.day)

    if not BIRTHDAY_CHANNEL_ID:
        await interaction.followup.send("❌ لازم تحدد BIRTHDAY_CHANNEL_ID بالكود الأول عشان تشتغل هذي الميزة.", ephemeral=True)
        return
    channel = guild.get_channel(BIRTHDAY_CHANNEL_ID)
    if not channel:
        await interaction.followup.send("❌ ما قدرت ألقى روم BIRTHDAY_CHANNEL_ID، تأكد من صحة الآيدي.", ephemeral=True)
        return

    role = guild.get_role(BIRTHDAY_ROLE_ID) if BIRTHDAY_ROLE_ID else None
    sent_count = 0
    today_str = today_riyadh.date().isoformat()

    async def celebrate(member: discord.Member, embed: discord.Embed):
        nonlocal sent_count
        try:
            await channel.send(content=member.mention, embed=embed)
            sent_count += 1
        except Exception as e:
            print(f"[Birthdays Catch-up] خطأ أثناء الإرسال: {e}")
        if role:
            try:
                await member.add_roles(role, reason="تعويض مناسبة خاصة فاتت هالسنة")
                record_special_role_grant(guild.id, member.id, today_str)
                if BIRTHDAY_ROLE_REWARD:
                    await add_balance(guild.id, member.id, BIRTHDAY_ROLE_REWARD, reason='مكافأة عيد الميلاد')
            except Exception:
                pass
        await asyncio.sleep(1)  # تجنّب الريت ليمت لو فيه أعضاء كثير

    # 1) أعياد الميلاد المسجّلة يدوياً واللي فاتت هالسنة
    with db_lock:
        cursor.execute("SELECT user_id, month, day FROM birthdays WHERE guild_id = ?", (guild.id,))
        birthday_rows = cursor.fetchall()
    for uid, month, day in birthday_rows:
        if (month, day) > today_tuple:
            continue  # لسا ما جاء وقتها هالسنة
        if already_celebrated(guild.id, uid, today_riyadh.year, "birthday"):
            continue
        member = guild.get_member(uid)
        if not member:
            continue
        embed = discord.Embed(
            title="🎂 عيد ميلاد سعيد! (تهنئة متأخرة)",
            description=f"كل عام وأنت بخير يا {member.mention} 🎉\nعذراً على التأخير، بس ما نسيناك! 🌟",
            color=discord.Color.from_rgb(255, 105, 180)
        )
        apply_branding(embed, guild, user=member)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="⏳ المدة", value=f"تم منحك رتبة <@&{BIRTHDAY_ROLE_ID}> لمدة يوم واحد 🎗️", inline=False)
        embed.add_field(name="🎁 مميزات الرول لليوم", value=(
            "📌 تثبيت وفك تثبيت أي رسالة\n"
            "🔇 كتم ونقل الأعضاء بالرومات الصوتية\n"
            "✏️ تغيير نك نيم أي عضو\n"
            "📢 مانشن @everyone / @here / كل الرولات\n"
            "🎤 أولوية بالتحدث بالفويس (Priority Speaker)\n"
            "⚡ تجاوز وضع الـ Slowmode"
        ), inline=False)
        await celebrate(member, embed)
        mark_celebrated(guild.id, uid, today_riyadh.year, "birthday")

    # 2) الذكرى السنوية للانضمام + عيد ميلاد إنشاء الحساب اللي فاتت هالسنة
    for member in guild.members:
        if member.bot:
            continue

        if member.joined_at:
            joined_riyadh = member.joined_at.astimezone(RIYADH_TZ)
            years = today_riyadh.year - joined_riyadh.year
            join_tuple = (joined_riyadh.month, joined_riyadh.day)
            if (years >= 1 and join_tuple <= today_tuple
                    and not already_celebrated(guild.id, member.id, today_riyadh.year, "join")):
                embed = discord.Embed(
                    title="🎉 ذكرى انضمام سعيدة! (تهنئة متأخرة)",
                    description=(
                        f"مرّت **{years}** سنة كاملة على انضمام {member.mention} لعائلة **{guild.name}**! 🥳\n"
                        "عذراً على التأخير، بس شكراً لبقائك معنا طول هالمدة 💙"
                    ),
                    color=discord.Color.from_rgb(88, 101, 242)
                )
                apply_branding(embed, guild, user=member)
                embed.set_thumbnail(url=member.display_avatar.url)
                embed.add_field(name="⏳ المدة", value=f"تم منحك رتبة <@&{BIRTHDAY_ROLE_ID}> لمدة يوم واحد 🎗️", inline=False)
                embed.add_field(name="🎁 مميزات الرول لليوم", value=(
                    "📌 تثبيت وفك تثبيت أي رسالة\n"
                    "🔇 كتم ونقل الأعضاء بالرومات الصوتية\n"
                    "✏️ تغيير نك نيم أي عضو\n"
                    "📢 مانشن @everyone / @here / كل الرولات\n"
                    "🎤 أولوية بالتحدث بالفويس (Priority Speaker)\n"
                    "⚡ تجاوز وضع الـ Slowmode"
                ), inline=False)
                await celebrate(member, embed)
                mark_celebrated(guild.id, member.id, today_riyadh.year, "join")

        created_riyadh = member.created_at.astimezone(RIYADH_TZ)
        acc_years = today_riyadh.year - created_riyadh.year
        created_tuple = (created_riyadh.month, created_riyadh.day)
        if (acc_years >= 1 and created_tuple <= today_tuple
                and not already_celebrated(guild.id, member.id, today_riyadh.year, "account")):
            embed = discord.Embed(
                title="🎈 عيد ميلاد حسابك في ديسكورد! (تهنئة متأخرة)",
                description=f"يصادف مرور **{acc_years}** سنة على إنشاء حساب {member.mention} في ديسكورد! 🎈",
                color=discord.Color.from_rgb(114, 137, 218)
            )
            apply_branding(embed, guild, user=member)
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="⏳ المدة", value=f"تم منحك رتبة <@&{BIRTHDAY_ROLE_ID}> لمدة يوم واحد 🎗️", inline=False)
            embed.add_field(name="🎁 مميزات الرول لليوم", value=(
                "📌 تثبيت وفك تثبيت أي رسالة\n"
                "🔇 كتم ونقل الأعضاء بالرومات الصوتية\n"
                "✏️ تغيير نك نيم أي عضو\n"
                "📢 مانشن @everyone / @here / كل الرولات\n"
                "🎤 أولوية بالتحدث بالفويس (Priority Speaker)\n"
                "⚡ تجاوز وضع الـ Slowmode"
            ), inline=False)
            await celebrate(member, embed)
            mark_celebrated(guild.id, member.id, today_riyadh.year, "account")

    await interaction.followup.send(f"✅ تم إرسال **{sent_count}** تهنئة متأخرة بروم {channel.mention}.", ephemeral=True)

# أمر تشخيصي مؤقت: يوريك كل عضو وتاريخ انضمامه وهل هو مؤهل لذكرى الانضمام/الحساب هالسنة ولا لأ ولماذا
@bot.tree.command(name="diagnose_anniversaries", description="[تشخيص] يعرض تفاصيل حساب أهلية كل عضو لذكرى الانضمام/الحساب (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@is_owner_only()
async def diagnose_anniversaries(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    today_riyadh = datetime.datetime.now(RIYADH_TZ)
    today_tuple = (today_riyadh.month, today_riyadh.day)

    lines = [f"📅 تاريخ اليوم (الرياض): {today_riyadh.date().isoformat()}\n"]
    total_members = 0
    eligible_join = 0
    eligible_account = 0

    for member in guild.members:
        if member.bot:
            continue
        total_members += 1

        entry = f"**{member.display_name}** (`{member.id}`)\n"

        if member.joined_at:
            joined_riyadh = member.joined_at.astimezone(RIYADH_TZ)
            years = today_riyadh.year - joined_riyadh.year
            join_tuple = (joined_riyadh.month, joined_riyadh.day)
            passed = join_tuple <= today_tuple
            already = already_celebrated(guild.id, member.id, today_riyadh.year, "join")
            is_eligible = (years >= 1 and passed and not already)
            if is_eligible:
                eligible_join += 1
            entry += (
                f"┗ انضمام: {joined_riyadh.date().isoformat()} | سنوات={years} | "
                f"مرّ تاريخه هالسنة={passed} | احتفلنا فيه={already} | "
                f"{'✅ مؤهل' if is_eligible else '❌ مو مؤهل'}\n"
            )
        else:
            entry += "┗ انضمام: ⚠️ ما فيه تاريخ انضمام محفوظ (member.joined_at = None)\n"

        created_riyadh = member.created_at.astimezone(RIYADH_TZ)
        acc_years = today_riyadh.year - created_riyadh.year
        created_tuple = (created_riyadh.month, created_riyadh.day)
        acc_passed = created_tuple <= today_tuple
        acc_already = already_celebrated(guild.id, member.id, today_riyadh.year, "account")
        acc_eligible = (acc_years >= 1 and acc_passed and not acc_already)
        if acc_eligible:
            eligible_account += 1
        entry += (
            f"┗ حساب: {created_riyadh.date().isoformat()} | سنوات={acc_years} | "
            f"مرّ تاريخه هالسنة={acc_passed} | احتفلنا فيه={acc_already} | "
            f"{'✅ مؤهل' if acc_eligible else '❌ مو مؤهل'}"
        )
        lines.append(entry)

    summary = (
        f"👥 إجمالي الأعضاء (بدون بوتات): {total_members}\n"
        f"🎉 مؤهلين لذكرى الانضمام الآن: {eligible_join}\n"
        f"🎈 مؤهلين لعيد ميلاد الحساب الآن: {eligible_account}\n"
        f"{'─' * 30}\n"
    )
    full_text = summary + "\n".join(lines)

    # نرسلها كملف نصي لو طويلة عشان ما تنكسر برسائل ديسكورد
    if len(full_text) > 1900:
        buffer = io.BytesIO(full_text.encode("utf-8"))
        await interaction.followup.send(
            content=f"👥 الأعضاء: {total_members} | 🎉 مؤهلين انضمام: {eligible_join} | 🎈 مؤهلين حساب: {eligible_account}",
            file=discord.File(buffer, filename="anniversary_diagnosis.txt"),
            ephemeral=True
        )
    else:
        await interaction.followup.send(full_text, ephemeral=True)

# أمر تحصين: يسجّل تاريخ اليوم لكل حامل رتبة مناسبات خاصة حالياً ما له تاريخ منح مسجّل
# مفيد تشغّله فوراً بعد أي تحديث بالكود يمس نظام المناسبات، عشان أي حامل رتبة حالي ما يتشال منه بالخطأ بأول تشغيل تلقائي جاي
@bot.tree.command(name="protect_current_role_holders", description="[صيانة] يحصّن حاملي رتبة المناسبات الحاليين من الحذف المبكر (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@is_owner_only()
async def protect_current_role_holders(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    role = guild.get_role(BIRTHDAY_ROLE_ID) if BIRTHDAY_ROLE_ID else None
    if not role:
        await interaction.followup.send("❌ ما لقيت رتبة المناسبات الخاصة (BIRTHDAY_ROLE_ID)، تأكد من الآيدي.", ephemeral=True)
        return

    today_str = datetime.datetime.now(RIYADH_TZ).date().isoformat()
    protected_count = 0
    already_tracked_count = 0

    for m in role.members:
        if get_special_role_grant_date(guild.id, m.id) == today_str:
            already_tracked_count += 1
            continue
        record_special_role_grant(guild.id, m.id, today_str)
        protected_count += 1

    await interaction.followup.send(
        f"✅ تم تحصين **{protected_count}** عضو (كانوا بدون تسجيل تاريخ منح).\n"
        f"ℹ️ **{already_tracked_count}** عضو كانوا مسجّلين مسبقاً أصلاً.\n"
        f"الحين ما راح تنشال رتبتهم إلا بعد يوم كامل من الآن.",
        ephemeral=True
    )

# أمر يعيد رتبة المناسبات الخاصة + يعيد إرسال رسالة التهنئة لأي عضو احتُفل فيه هالسنة
# لكن الرتبة مو موجودة عنده حالياً (مثلاً لأنها انحذفت بالخطأ، أو الرسالة الأصلية انحذفت من الروم)
@bot.tree.command(name="test_adhan", description="[اختبار] تشغيل صوت الأذان الآن بالرومات الصوتية اللي فيها أعضاء (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@is_owner_only()
async def test_adhan(interaction: discord.Interaction, prayer: str = "Dhuhr"):
    await interaction.response.defer(ephemeral=True)

    if not ADHAN_VOICE_ENABLED:
        await interaction.followup.send("⚠️ ميزة تشغيل الأذان معطّلة حالياً (ADHAN_VOICE_ENABLED = False).", ephemeral=True)
        return

    if _build_adhan_source(prayer) is None:
        await interaction.followup.send(
            f"❌ ما قدرت أجهّز ملف الأذان.\n"
            f"• تأكد إن المسار موجود: `{ADHAN_AUDIO_SOURCE}`\n"
            f"• وتأكد إن **ffmpeg** مثبّت على الاستضافة و **PyNaCl** مثبّتة.",
            ephemeral=True
        )
        return

    asyncio.create_task(play_adhan_in_guild(interaction.guild, prayer))
    await interaction.followup.send("🕌 بديت تشغيل الأذان بالرومات الصوتية اللي فيها أعضاء.", ephemeral=True)


@bot.tree.command(name="restore_missing_roles", description="[صيانة] يعيد الرتبة والتهنئة لمن احتُفل فيه هالسنة وما عنده الرتبة (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@is_owner_only()
async def restore_missing_roles(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    role = guild.get_role(BIRTHDAY_ROLE_ID) if BIRTHDAY_ROLE_ID else None
    if not role:
        await interaction.followup.send("❌ ما لقيت رتبة المناسبات الخاصة (BIRTHDAY_ROLE_ID)، تأكد من الآيدي.", ephemeral=True)
        return
    channel = guild.get_channel(BIRTHDAY_CHANNEL_ID) if BIRTHDAY_CHANNEL_ID else None

    today_riyadh = datetime.datetime.now(RIYADH_TZ)
    today_str = today_riyadh.date().isoformat()
    year = today_riyadh.year
    restored = []

    for member in guild.members:
        if member.bot or role in member.roles:
            continue

        kinds = []
        if already_celebrated(guild.id, member.id, year, "birthday"):
            kinds.append("birthday")
        if already_celebrated(guild.id, member.id, year, "join"):
            kinds.append("join")
        if already_celebrated(guild.id, member.id, year, "account"):
            kinds.append("account")
        if not kinds:
            continue

        for kind in kinds:
            if kind == "birthday":
                embed = discord.Embed(
                    title="🎂 عيد ميلاد سعيد!",
                    description=(
                        f"كل عام وأنت بخير يا {member.mention} 🎉\n"
                        "نتمنى لك سنة مليئة بالصحة والسعادة والنجاح، ويسعدنا وجودك بمجتمعنا. 🌟"
                    ),
                    color=discord.Color.from_rgb(255, 105, 180)
                )
            elif kind == "join" and member.joined_at:
                joined_riyadh = member.joined_at.astimezone(RIYADH_TZ)
                years = today_riyadh.year - joined_riyadh.year
                embed = discord.Embed(
                    title="🎉 ذكرى انضمام سعيدة!",
                    description=(
                        f"مرّت **{years}** سنة كاملة على انضمام {member.mention} لعائلة **{guild.name}**! 🥳\n"
                        "شكراً لبقائك معنا طول هالمدة 💙"
                    ),
                    color=discord.Color.from_rgb(88, 101, 242)
                )
            elif kind == "account":
                created_riyadh = member.created_at.astimezone(RIYADH_TZ)
                acc_years = today_riyadh.year - created_riyadh.year
                embed = discord.Embed(
                    title="🎈 عيد ميلاد حسابك في ديسكورد!",
                    description=f"اليوم يصادف مرور **{acc_years}** سنة على إنشاء حساب {member.mention} في ديسكورد! 🎈",
                    color=discord.Color.from_rgb(114, 137, 218)
                )
            else:
                continue

            apply_branding(embed, guild, user=member)
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="⏳ المدة", value=f"تم منحك رتبة <@&{BIRTHDAY_ROLE_ID}> لمدة يوم واحد 🎗️", inline=False)
            embed.add_field(name="🎁 مميزات الرول لليوم", value=(
                "📌 تثبيت وفك تثبيت أي رسالة\n"
                "🔇 كتم ونقل الأعضاء بالرومات الصوتية\n"
                "✏️ تغيير نك نيم أي عضو\n"
                "📢 مانشن @everyone / @here / كل الرولات\n"
                "🎤 أولوية بالتحدث بالفويس (Priority Speaker)\n"
                "⚡ تجاوز وضع الـ Slowmode"
            ), inline=False)
            if channel:
                try:
                    await channel.send(content=member.mention, embed=embed)
                    await asyncio.sleep(1)  # تجنّب الريت ليمت لو فيه أعضاء كثير
                except Exception as e:
                    print(f"[Birthdays] خطأ أثناء إعادة إرسال رسالة التهنئة لـ {member.id}: {e}")

        try:
            await member.add_roles(role, reason="استرجاع رتبة مناسبة خاصة + إعادة رسالة التهنئة")
            record_special_role_grant(guild.id, member.id, today_str)
            if BIRTHDAY_ROLE_REWARD:
                await add_balance(guild.id, member.id, BIRTHDAY_ROLE_REWARD, reason='مكافأة عيد الميلاد')
            restored.append(member.mention)
        except Exception as e:
            print(f"[Birthdays] خطأ أثناء استرجاع الرتبة لـ {member.id}: {e}")

    if not restored:
        await interaction.followup.send("ℹ️ ما فيه أي عضو يحتاج استرجاع حالياً — الكل إما عنده الرتبة أو ما احتُفل فيه هالسنة أصلاً.", ephemeral=True)
        return

    text = f"✅ تم استرجاع الرتبة وإعادة إرسال التهنئة لـ **{len(restored)}** عضو:\n" + "، ".join(restored)
    if len(text) > 1900:
        buffer = io.BytesIO(text.encode("utf-8"))
        await interaction.followup.send(
            content=f"✅ تم استرجاع الرتبة وإعادة إرسال التهنئة لـ **{len(restored)}** عضو (القائمة بالملف المرفق).",
            file=discord.File(buffer, filename="restored_members.txt"),
            ephemeral=True
        )
    else:
        await interaction.followup.send(text, ephemeral=True)

# ----------------- الأحداث -----------------
@bot.event
async def on_ready():
    print("✅ ON_READY START", flush=True)
    global _ready_initialized
    async with _ready_initialization_lock:
        if _ready_initialized:
            return
        try:
            await initialize_ready_once()
            _ready_initialized = True
            print("✅ ON_READY FINISHED", flush=True)
        except Exception as exc:
            print(f"❌ ON_READY ERROR: {exc}", flush=True)
            import traceback
            traceback.print_exc()

async def initialize_ready_once():
    if not request_monitor_task.is_running():
        request_monitor_task.start()
    if not public_control_panel_guide_task.is_running():
        public_control_panel_guide_task.start()
    if not public_quest_reminder_task.is_running():
        public_quest_reminder_task.start()
    global _clip_panel_refresh_job
    _game_seed_time = time.time()
    _game_snapshots = [(guild.id, member.id, {key: (name, None) for key, (name, start) in get_playing_games(member, _game_seed_time).items()})
                       for guild in bot.guilds for member in guild.members
                       if not member.bot and member.activities and member.voice and member.voice.channel]
    await asyncio.to_thread(seed_game_activity_sync, _game_snapshots, _game_seed_time)
    if _clip_panel_refresh_job is None or _clip_panel_refresh_job.done():
        _clip_panel_refresh_job = asyncio.create_task(refresh_active_clip_vote_panels())
    if not local_safety_backup_task.is_running():
        local_safety_backup_task.start()
    if not suggestion_workflow_task.is_running():
        suggestion_workflow_task.start()
    if not quest_reward_recovery_task.is_running():
        quest_reward_recovery_task.start()
    if not personal_report_cleanup_task.is_running():
        personal_report_cleanup_task.start()
    if not personal_weekly_report_task.is_running():
        personal_weekly_report_task.start()
    bot.add_view(RocketLeagueRolesView())
    bot.add_view(ControlPanelView())
    bot.add_view(AdminControlPanelView())
    bot.add_view(ServerRulesView())
    _auto_reply_channel = bot.get_channel(AUTO_RESPONSE_CHANNEL_ID)
    if _auto_reply_channel:
        await asyncio.to_thread(seed_auto_responses_sync, _auto_reply_channel.guild.id)
    bot.add_view(TicketControlView())
    bot.add_view(TicketTypeSelectView())
    bot.add_view(SuggestionVoteView())
    bot.add_view(ClipVoteView())
    bot.add_view(RoulettePanelView())
    bot.add_view(WarNameView())
    for _leader_panel_channel_id in LEADER_DEPUTY_VOICE_PANEL_CHANNEL_IDS:
        bot.add_view(LeaderDeputyVoiceControlView(channel_id=_leader_panel_channel_id))

    # تسجيل أزرار شراء المتجر كـ Views دائمة عشان تشتغل حتى بعد إعادة تشغيل البوت
    try:
        all_active_items = await get_active_shop_items()
        for chunk_start in range(0, len(all_active_items), 25):
            bot.add_view(build_shop_view(all_active_items[chunk_start:chunk_start + 25]))
    except Exception as e:
        print(f"[Shop] خطأ بتسجيل أزرار المتجر الدائمة: {e}")

    try:
        manifest = [command.to_dict(bot.tree) for command in bot.tree.get_commands()]
        signature = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        state_key = f"slash_command_signature:{bot.user.id}"
        if get_bot_state(state_key) != signature:
            synced = await bot.tree.sync()
            set_bot_state(state_key, signature)
            print(f"Synced {len(synced)} slash command(s)")
    except discord.HTTPException as exc:
        print(f"[Commands] Sync failed: HTTP {exc.status}; will retry at next startup.")

    if not stats_task.is_running():
        stats_task.start()
    if not voice_xp_task.is_running():
        voice_xp_task.start()
    if not afk_check_task.is_running():
        afk_check_task.start()
    if not warning_expiry_task.is_running():
        warning_expiry_task.start()
    if not check_free_games_task.is_running():
        check_free_games_task.start()
    if not weekly_leaderboard_task.is_running():
        weekly_leaderboard_task.start()
    if not money_leaderboard_task.is_running():
        money_leaderboard_task.start()
    if not royal_war_task.is_running():
        royal_war_task.start()
    if not weekly_best_clip_task.is_running():
        weekly_best_clip_task.start()
    if not birthday_task.is_running():
        birthday_task.start()
    if not db_backup_task.is_running():
        db_backup_task.start()
    if not monthly_salary_task.is_running():
        monthly_salary_task.start()
    if not prayer_reminder_task.is_running():
        prayer_reminder_task.start()
    if not morning_adhkar_task.is_running():
        morning_adhkar_task.start()
    if not evening_adhkar_task.is_running():
        evening_adhkar_task.start()
    if not salawat_task.is_running():
        salawat_task.start()
    if not hadith_task.is_running():
        hadith_task.start()

    # تحميل/تحديث كاش الدعوات لكل سيرفر عشان يقدر نظام تتبع الدعوات يعرف مين دعا مين لاحقاً
    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            invite_cache[guild.id] = {
                inv.code: {"uses": inv.uses or 0, "inviter_id": inv.inviter.id if inv.inviter else None, "max_uses": inv.max_uses or 0}
                for inv in invites
            }
        except discord.Forbidden:
            print(f"[Invite Tracker] ما فيه صلاحية Manage Server بسيرفر {guild.name}، تتبع الدعوات معطّل فيه.")
        except Exception as e:
            print(f"[Invite Tracker] خطأ أثناء تحميل الدعوات لسيرفر {guild.name}: {e}")

    print(f"Logged in as {bot.user}")
    print("------ Bot is Ready ------")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    await process_auto_response(message)

    user_id = message.author.id
    current_time = time.time()

    # ----- تسجيل الحضور اليومي (مرة وحدة باليوم لكل عضو) -----
    asyncio.create_task(process_daily_checkin(message.author))
    asyncio.create_task(bump_quest(message.guild, message.author, "daily_chat", 1))

    if user_id not in text_xp_cooldowns or current_time - text_xp_cooldowns[user_id] >= TEXT_XP_COOLDOWN_SECONDS:
        text_xp_cooldowns[user_id] = current_time
        xp_gain = random.randint(10, 20)
        new_level, leveled_up, _, levels_gained = await add_user_xp(message.guild.id, user_id, xp_gain, is_voice=False, track_quests=True)

        if leveled_up:
            reward = levels_gained * LEVEL_UP_REWARD
            if reward > 0:
                await add_balance(message.guild.id, user_id, reward, reason='مكافأة ترقية المستوى')
            level_channel = bot.get_channel(LEVEL_CHANNEL_ID)
            if level_channel:
                embed = create_levelup_embed(message.author, new_level, message.guild, reward=reward)
                await level_channel.send(embed=embed)
            await update_member_level_role(message.author, new_level)

    # ----- مكافأة إرسال فيديو أو صورة بالروم المخصص (مرفقات أو روابط زي Medal/Streamable/Outplayed) -----
    if message.channel.id == VIDEO_REWARD_CHANNEL_ID:
        await handle_clip_message(message, user_id)

    await bot.process_commands(message)

# ----------------- الردود التلقائية حسب الكلمات -----------------
_auto_response_cache = {}
_auto_response_user_cooldowns = {}
_auto_response_channel_cooldowns = {}
AUTO_RESPONSE_USER_COOLDOWN = 30
AUTO_RESPONSE_CHANNEL_COOLDOWN = 10
AUTO_RESPONSE_CHANNEL_ID = 1530739438517223516
DEFAULT_AUTO_RESPONSES = [
    ('السلام عليكم', 'وعليكم السلام ورحمة الله وبركاته 🌷'),
    ('سلام عليكم', 'وعليكم السلام ورحمة الله وبركاته 🌷'),
    ('هلا', 'هلا وغلا، حيّاك الله 👋'),
    ('مرحبا', 'يا مرحبا، نورت الشات 👋'),
    ('صباح الخير', 'صباح النور والسرور ☀️'),
    ('مساء الخير', 'مساء النور، حيّاك الله 🌙'),
    ('تصبحون على خير', 'وأنت من أهل الخير، نوم العوافي 🌙'),
    ('تصبح على خير', 'وأنت من أهل الخير، نوم العوافي 🌙'),
    ('شكرا', 'العفو، ما سوّينا إلا الواجب 🌷'),
    ('مشكور', 'العفو، تسلم 🌷'),
    ('يعطيك العافية', 'الله يعافيك ويسعدك 🌷'),
    ('يعطيكم العافية', 'الله يعافيك ويسعدك 🌷'),
    ('مبروك', 'الله يبارك فيك، وعقبال الجميع 🎉'),
    ('gg', 'GG! يعطيكم العافية على اللعب 🎮'),
    ('وين القوانين', 'تلقى القوانين في لوحة القوانين: اختر «القوانين الأساسية» أو «قوانين ستيم» 📜'),
    ('كيف افتح تكت', 'اضغط «الدعم الفني» في لوحة التحكم، ثم اختر نوع التذكرة 🛠️'),
    ('كيف اشوف مهامي', 'اضغط «مهامي» في لوحة التحكم لعرض مهامك اليومية والأسبوعية وتقدمك 🧭'),
    ('كيف اشوف رصيدي', 'اضغط «كشف البنك» في لوحة التحكم، أو استخدم `/balance` 💰'),
]


def normalize_auto_response_text(text):
    import re
    import unicodedata
    text = unicodedata.normalize('NFKC', text).casefold()
    text = ''.join(char for char in text if unicodedata.category(char) != 'Mn' and char != 'ـ')
    text = text.translate(str.maketrans({'أ': 'ا', 'إ': 'ا', 'آ': 'ا'}))
    return re.sub(r'\s+', ' ', text).strip()


def auto_response_matches(content, trigger, mode):
    import re
    content = normalize_auto_response_text(content)
    trigger = normalize_auto_response_text(trigger)
    if not trigger:
        return False
    if mode == 'exact':
        return content == trigger
    if mode == 'contains':
        return trigger in content
    return re.search(r'(?<!\w)' + re.escape(trigger) + r'(?!\w)', content) is not None


def get_auto_responses_sync(guild_id):
    with db_lock:
        if guild_id not in _auto_response_cache:
            _auto_response_cache[guild_id] = conn.execute(
                'SELECT trigger_word, response_text, match_mode, channel_id, enabled '
                'FROM auto_responses WHERE guild_id = ?', (guild_id,)
            ).fetchall()
        return list(_auto_response_cache[guild_id])


def seed_auto_responses_sync(guild_id):
    # مرة واحدة فقط: إعادة التشغيل تحفظ تعديلات الإدارة وحذفها للردود الجاهزة.
    key = f'auto_responses_defaults_v1:{guild_id}'
    with db_lock:
        if conn.execute('SELECT value FROM bot_state WHERE key=?', (key,)).fetchone():
            return
        for trigger, response in DEFAULT_AUTO_RESPONSES:
            conn.execute(
                'INSERT OR IGNORE INTO auto_responses '
                '(guild_id, trigger_word, response_text, match_mode, channel_id, enabled) '
                'VALUES (?, ?, ?, ?, ?, 1)',
                (guild_id, normalize_auto_response_text(trigger), response, 'word', AUTO_RESPONSE_CHANNEL_ID)
            )
        conn.execute('INSERT INTO bot_state (key, value) VALUES (?, ?)', (key, 'done'))
        conn.commit()
        _auto_response_cache.pop(guild_id, None)


def save_auto_response_sync(guild_id, trigger, response, match_mode, channel_id):
    with db_lock:
        conn.execute(
            'INSERT INTO auto_responses (guild_id, trigger_word, response_text, match_mode, channel_id, enabled) '
            'VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT(guild_id, trigger_word) DO UPDATE SET '
            'response_text=excluded.response_text, match_mode=excluded.match_mode, '
            'channel_id=excluded.channel_id, enabled=1',
            (guild_id, trigger, response, match_mode, channel_id)
        )
        conn.commit()
        _auto_response_cache.pop(guild_id, None)


def delete_auto_response_sync(guild_id, trigger):
    with db_lock:
        result = conn.execute('DELETE FROM auto_responses WHERE guild_id=? AND trigger_word=?', (guild_id, trigger))
        conn.commit()
        _auto_response_cache.pop(guild_id, None)
        return result.rowcount > 0


async def process_auto_response(message):
    if not message.guild or message.author.bot or message.webhook_id or not message.content:
        return
    if message.channel.id != AUTO_RESPONSE_CHANNEL_ID:
        return
    now = time.monotonic()
    user_key = (message.guild.id, message.author.id)
    channel_key = (message.guild.id, message.channel.id)
    if now < _auto_response_user_cooldowns.get(user_key, 0) or now < _auto_response_channel_cooldowns.get(channel_key, 0):
        return
    try:
        rules = await asyncio.to_thread(get_auto_responses_sync, message.guild.id)
        # الرد المخصص للروم له الأولوية، ثم العبارة الأطول؛ رد واحد لكل رسالة.
        rules.sort(key=lambda row: (row[3] is not None, len(row[0])), reverse=True)
        for trigger, response, mode, channel_id, enabled in rules:
            if not enabled or (channel_id is not None and channel_id != message.channel.id):
                continue
            if not auto_response_matches(message.content, trigger, mode):
                continue
            # إعادة الفحص بعد قراءة القاعدة تمنع ردوداً متزامنة تتجاوز المهلة.
            if now < _auto_response_user_cooldowns.get(user_key, 0) or now < _auto_response_channel_cooldowns.get(channel_key, 0):
                return
            _auto_response_user_cooldowns[user_key] = now + AUTO_RESPONSE_USER_COOLDOWN
            _auto_response_channel_cooldowns[channel_key] = now + AUTO_RESPONSE_CHANNEL_COOLDOWN
            for cooldowns in (_auto_response_user_cooldowns, _auto_response_channel_cooldowns):
                if len(cooldowns) > 2000:
                    for key in [key for key, expires in cooldowns.items() if expires <= now]:
                        cooldowns.pop(key, None)
            await message.reply(response, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            return
    except Exception as exc:
        # فشل الرد لا يوقف حساب النشاط والخبرة أو بقية معالجة الرسالة.
        print(f'[Auto Response] {exc}')


@bot.tree.command(name='auto_reply_set', description='إضافة أو تعديل رد تلقائي عند كتابة كلمة أو عبارة')
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(trigger='الكلمة أو العبارة التي تفعّل الرد', response='رد البوت عليها',
                       match_mode='طريقة المطابقة (الافتراضي كلمة أو عبارة داخل الرسالة)')
@app_commands.choices(match_mode=[
    app_commands.Choice(name='كلمة أو عبارة مستقلة داخل الرسالة', value='word'),
    app_commands.Choice(name='الرسالة كاملة تطابق العبارة', value='exact'),
    app_commands.Choice(name='العبارة في أي جزء من الرسالة', value='contains'),
])
@is_owner_or_deputy()
async def auto_reply_set(interaction: discord.Interaction, trigger: str, response: str,
                         match_mode: str = 'word'):
    trigger = normalize_auto_response_text(trigger)
    response = response.strip()
    if not trigger or len(trigger) > 100 or not response or len(response) > 1800:
        await interaction.response.send_message('❌ العبارة من 1 إلى 100 حرف، والرد من 1 إلى 1800 حرف.', ephemeral=True)
        return
    if match_mode not in ('word', 'exact', 'contains'):
        await interaction.response.send_message('❌ طريقة المطابقة غير صحيحة.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await asyncio.to_thread(save_auto_response_sync, interaction.guild.id, trigger, response,
                            match_mode, AUTO_RESPONSE_CHANNEL_ID)
    scope = f'<#{AUTO_RESPONSE_CHANNEL_ID}>'
    await interaction.followup.send(f'✅ تم حفظ الرد على **{trigger}** في {scope}.',
                                    ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name='auto_reply_delete', description='حذف رد تلقائي محفوظ')
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(trigger='الكلمة أو العبارة المطلوب حذف ردها')
@is_owner_or_deputy()
async def auto_reply_delete(interaction: discord.Interaction, trigger: str):
    await interaction.response.defer(ephemeral=True)
    removed = await asyncio.to_thread(delete_auto_response_sync, interaction.guild.id,
                                      normalize_auto_response_text(trigger))
    await interaction.followup.send('✅ تم حذف الرد.' if removed else 'ما فيه رد محفوظ لهذه العبارة.', ephemeral=True)


@bot.tree.command(name='auto_reply_list', description='عرض الكلمات والردود التلقائية المحفوظة بالسيرفر')
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@is_owner_or_deputy()
async def auto_reply_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    rules = await asyncio.to_thread(get_auto_responses_sync, interaction.guild.id)
    if not rules:
        await interaction.followup.send('ما فيه ردود محفوظة. أضفها من `/auto_reply_set` أو قسم الرومات والنشر في لوحة الإدارة.', ephemeral=True)
        return
    lines = []
    for trigger, response, mode, channel_id, enabled in rules:
        scope = f'<#{channel_id}>' if channel_id else 'كل الرومات'
        lines.append(f'• {trigger} | {mode} | {scope}\n  {response}')
    await interaction.followup.send('📋 الردود المحفوظة:',
        file=discord.File(io.BytesIO('\n\n'.join(lines).encode('utf-8-sig')), filename='auto_replies.txt'), ephemeral=True)


ADMIN_PANEL_ACTIONS = {
    'members': ('إدارة الأعضاء', '👥', [
        ('warnings', 'سجل الإنذارات'), ('warn', 'إعطاء إنذار'),
        ('remove_warning', 'حذف إنذار'), ('clear_warnings', 'مسح إنذارات عضو'),
        ('modlogs', 'السجل الإداري'), ('invites', 'دعوات العضو'),
        ('profile', 'بروفايل عضو'), ('rank', 'بطاقة مستوى عضو')]),
    'channels': ('الرومات والنشر', '📢', [
        ('lock', 'قفل روم'), ('unlock', 'فتح روم'),
        ('clear', 'تنظيف رسائل الروم'), ('embed', 'إيمبيد مع صورة من جهازك'),
        ('decree', 'مرسوم رسمي'), ('announce_features', 'إعلان الميزات'),
        ('server_rules', 'نشر لوحة القوانين'), ('control_panel', 'نشر لوحة الأعضاء'),
        ('setup_leader_voice_panel', 'نشر لوحة الفويس للإدارة'),
        ('setup_roulette_panel', 'نشر لوحة الروليت'),
        ('steam_family_rules', 'عرض قوانين ستيم'),
        ('auto_reply_set', 'إضافة أو تعديل رد تلقائي'),
        ('auto_reply_delete', 'حذف رد تلقائي'),
        ('auto_reply_list', 'عرض الردود التلقائية')]),
    'teams': ('الفرق والمتصدرون', '⚔️', [
        ('houses', 'ترتيب الفرق'), ('clip_voters_allowed', 'المسموح لهم بتصويت اللقطات'), ('royal_war_start', 'بدء حرب جديدة'),
        ('start_teams', 'توزيع الفرق'), ('royal_war_end', 'إنهاء الحرب وصرف الجوائز'),
        ('leaderboard', 'لوحة المستويات'), ('post_weekly_leaderboard', 'نشر متصدري الأسبوع'),
        ('post_money_leaderboard', 'نشر أغنى الأعضاء')]),
    'economy': ('البنك والمستويات', '💰', [
        ('balance', 'رصيد عضو'), ('all_balances', 'أرصدة جميع الأعضاء'),
        ('add_balance', 'إضافة ريالات'), ('remove_balance', 'خصم ريالات'),
        ('add_xp', 'إضافة خبرة'), ('remove_xp', 'خصم خبرة'),
        ('set_level', 'تحديد مستوى'), ('reset_level', 'تصفير مستوى'),
        ('shop', 'نشر متجر'), ('shop_remove_item', 'إيقاف عنصر بالمتجر'),
        ('pay_salaries_now', 'صرف الرواتب')]),
    'maintenance': ('الحفظ والصيانة', '🛠️', [
        ('backup_now', 'نسخة احتياطية الآن'), ('prepare_update', 'الحفظ قبل الإغلاق والتحديث'),
        ('fix_xp_desync', 'تصحيح توافق الخبرة والمستوى'),
        ('check_free_games_now', 'فحص الألعاب المجانية'),
        ('diagnose_anniversaries', 'تشخيص المناسبات'),
        ('catch_up_anniversaries', 'تعويض تهاني المناسبات'),
        ('restore_missing_roles', 'استعادة رتب المناسبات'),
        ('protect_current_role_holders', 'حماية رتب المناسبات الحالية')]),
}


def can_use_admin_panel(interaction):
    return bool(interaction.guild and (
        is_staff_member(interaction.user) or interaction.user.guild_permissions.administrator
    ))


async def admin_action_allowed(interaction, command):
    if not can_use_admin_panel(interaction) or command is None:
        return False
    try:
        for predicate in command.checks:
            if not await discord.utils.maybe_coroutine(predicate, interaction):
                return False
    except app_commands.CheckFailure:
        return False
    return True


class AdminPrivateView(ButtonLimitedView):
    def __init__(self, user_id, timeout=180):
        super().__init__(timeout=timeout)
        self.user_id = user_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id or not can_use_admin_panel(interaction):
            await interaction.response.send_message('❌ هذه القائمة للإداري الذي فتحها فقط.', ephemeral=True)
            return False
        return True


class AdminActionConfirmView(AdminPrivateView):
    def __init__(self, user_id, command, values):
        super().__init__(user_id)
        self.command = command
        self.values = values
        self.used = False

    @discord.ui.button(label='تأكيد التنفيذ', emoji='✅', style=discord.ButtonStyle.primary)
    async def confirm(self, interaction, button):
        if self.used:
            await interaction.response.send_message('تم استخدام هذا التأكيد؛ افتح الأداة من جديد.', ephemeral=True)
            return
        if not await admin_action_allowed(interaction, self.command):
            await interaction.response.send_message('❌ ما عندك صلاحية تنفيذ هذه الأداة.', ephemeral=True)
            return
        if self.used:
            await interaction.response.send_message('تم استخدام هذا التأكيد؛ افتح الأداة من جديد.', ephemeral=True)
            return
        self.used = True
        self.stop()
        try:
            # تنفيذ نفس الدالة الأصلية، بما فيها التحقق والتسجيل وحفظ البيانات.
            await self.command.callback(interaction, **self.values)
        except Exception as exc:
            print(f'[Admin Panel] /{self.command.name}: {exc}')
            message = '❌ تعذر إكمال العملية؛ راجع سجل التشغيل قبل تكرارها.'
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        try:
            await interaction.message.edit(view=None)
        except discord.HTTPException:
            pass

    @discord.ui.button(label='إلغاء', style=discord.ButtonStyle.primary)
    async def cancel(self, interaction, button):
        self.used = True
        self.stop()
        await interaction.response.edit_message(content='تم إلغاء العملية.', embed=None, view=None)


async def show_admin_confirmation(interaction, command, values):
    details = '\n'.join(f'**{key}:** {getattr(value, "mention", value)}' for key, value in values.items())[:3000]
    embed = discord.Embed(
        title=f'تأكيد /{command.name}',
        description=(command.description + '\n\n' + (details or 'تنفيذ بالإعدادات الافتراضية.')
                     + f'\n\n**روم التنفيذ:** {interaction.channel.mention}\nراجع البيانات قبل التأكيد.'),
        color=discord.Color.blue()
    )
    await interaction.response.send_message(
        embed=embed, view=AdminActionConfirmView(interaction.user.id, command, values), ephemeral=True
    )


class AdminActionModal(discord.ui.Modal):
    def __init__(self, command, user_id):
        super().__init__(title=f'إعداد /{command.name}'[:45], timeout=300)
        self.command = command
        self.user_id = user_id
        self.inputs = []
        for parameter in command.parameters:
            kind = parameter.type.name
            hint = parameter.description
            if kind in ('user', 'channel', 'role', 'mentionable'):
                hint = 'اكتب الآيدي أو المنشن'
            elif kind == 'boolean':
                hint = 'نعم / لا'
            if parameter.choices:
                hint = ' / '.join(str(choice.value) for choice in parameter.choices)
            field = discord.ui.TextInput(
                label=parameter.name[:45], placeholder=hint[:100],
                required=parameter.required, max_length=1000,
                style=discord.TextStyle.paragraph if kind == 'string' else discord.TextStyle.short
            )
            self.add_item(field)
            self.inputs.append((parameter, field))

    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id or not await admin_action_allowed(interaction, self.command):
            await interaction.response.send_message('❌ ما عندك صلاحية تنفيذ هذه الأداة.', ephemeral=True)
            return
        values = {}
        try:
            for parameter, field in self.inputs:
                raw = field.value.strip()
                if not raw and not parameter.required:
                    continue
                kind = parameter.type.name
                value = raw
                if kind == 'integer':
                    value = int(raw)
                elif kind == 'number':
                    value = float(raw)
                    if not math.isfinite(value):
                        raise ValueError('الرقم غير صحيح')
                elif kind == 'boolean':
                    if raw.lower() not in ('نعم', 'لا', 'true', 'false', '1', '0'):
                        raise ValueError('اكتب نعم أو لا')
                    value = raw.lower() in ('نعم', 'true', '1')
                elif kind in ('user', 'channel', 'role', 'mentionable'):
                    entity_id = int(raw.strip('<@!&#>'))
                    if kind == 'user':
                        value = interaction.guild.get_member(entity_id) or await interaction.guild.fetch_member(entity_id)
                    elif kind == 'channel':
                        value = interaction.guild.get_channel(entity_id)
                        if not isinstance(value, discord.TextChannel) or not value.permissions_for(interaction.user).view_channel:
                            raise ValueError('حدد روم كتابي تقدر تشوفه داخل السيرفر')
                    elif kind == 'role':
                        value = interaction.guild.get_role(entity_id)
                    else:
                        value = interaction.guild.get_member(entity_id) or interaction.guild.get_role(entity_id)
                    if value is None:
                        raise ValueError('الآيدي غير موجود بالسيرفر')
                if parameter.choices and value not in [choice.value for choice in parameter.choices]:
                    raise ValueError('اختر إحدى القيم المكتوبة في الخانة')
                if kind in ('integer', 'number'):
                    if parameter.min_value is not None and value < parameter.min_value:
                        raise ValueError('الرقم أقل من الحد المسموح')
                    if parameter.max_value is not None and value > parameter.max_value:
                        raise ValueError('الرقم أكبر من الحد المسموح')
                values[parameter.name] = value
        except (ValueError, discord.HTTPException) as exc:
            await interaction.response.send_message(f'❌ راجع المدخلات: {exc}', ephemeral=True)
            return
        await show_admin_confirmation(interaction, self.command, values)


class AdminActionsView(AdminPrivateView):
    def __init__(self, user_id, actions):
        super().__init__(user_id)
        for index, (command_name, label) in enumerate(actions):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, row=index // 5)
            button.callback = self.make_callback(command_name)
            self.add_item(button)

    def make_callback(self, command_name):
        async def callback(interaction):
            command = bot.tree.get_command(command_name)
            if not await admin_action_allowed(interaction, command):
                await interaction.response.send_message('❌ هذه الأداة غير متاحة لصلاحياتك.', ephemeral=True)
                return
            # رفع الملفات وإعداد المتجر الكبير يحتاجان واجهة أمر ديسكورد الأصلية.
            if len(command.parameters) > 5 or any(p.type.name == 'attachment' for p in command.parameters):
                guide = '\n'.join(f'• **{p.name}**: {p.description}' for p in command.parameters)
                await interaction.response.send_message(
                    f'استخدم **/{command.name}** في روم التنفيذ لتفتح إعداداته الكاملة.\n'
                    + ('في `/embed` اختر **image** وارفع الصورة من جهازك.\n' if command.name == 'embed' else '')
                    + guide, ephemeral=True
                )
            elif command.parameters:
                await interaction.response.send_modal(AdminActionModal(command, interaction.user.id))
            else:
                await show_admin_confirmation(interaction, command, {})
        return callback


class AdminControlPanelView(ControlPanelView):
    def __init__(self):
        super().__init__()
        # معرّفات مستقلة حتى لا تتداخل مع أزرار لوحة الأعضاء بعد إعادة التشغيل.
        for item in self.children:
            if item.custom_id:
                item.custom_id = 'admin_' + item.custom_id
        for key, (label, emoji, actions) in ADMIN_PANEL_ACTIONS.items():
            button = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.primary,
                                       custom_id=f'admin_category_{key}', row=4)
            button.callback = self.make_category_callback(key)
            self.add_item(button)

    async def interaction_check(self, interaction):
        if not can_use_admin_panel(interaction):
            await interaction.response.send_message('❌ لوحة مخصصة للإدارة فقط.', ephemeral=True)
            return False
        return True

    def make_category_callback(self, key):
        async def callback(interaction):
            label, emoji, catalog = ADMIN_PANEL_ACTIONS[key]
            actions = []
            for name, title in catalog:
                if await admin_action_allowed(interaction, bot.tree.get_command(name)):
                    actions.append((name, title))
            if not actions:
                await interaction.response.send_message('ما عندك أدوات مسموحة في هذا القسم حسب صلاحياتك الحالية.', ephemeral=True)
                return
            await interaction.response.send_message(
                f'{emoji} **{label}**\nاختر الأداة. الأدوات المعروضة حسب صلاحياتك الحالية.\n'
                'في أدوات الرومات تقدر تحدد آيدي الروم؛ تركه فارغاً يستهدف روم اللوحة.',
                view=AdminActionsView(interaction.user.id, actions), ephemeral=True
            )
        return callback


@bot.tree.command(name='admin_panel', description='إرسال لوحة الإدارة: خدمات الأعضاء وأدوات الإدارة والفرق والبنك والصيانة')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel='روم الإدارة المطلوب إرسال اللوحة فيه (اختياري)')
async def admin_panel_command(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not can_use_admin_panel(interaction):
        await interaction.response.send_message('❌ هذا الأمر للإدارة فقط.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    target = channel or interaction.channel
    embed = discord.Embed(
        title='🛡️ لوحة تحكم الإدارة',
        description=(
            'كل خدمات لوحة الأعضاء بالأعلى، وأقسام الإدارة في الصف الأخير.\n\n'
            '👥 **إدارة الأعضاء:** الإنذارات والسجل الإداري والدعوات وبطاقات الأعضاء.\n'
            '📢 **الرومات والنشر:** قفل وفتح وتنظيف الروم، الإعلانات والقوانين ولوحات السيرفر، وإدارة الردود التلقائية بالشات.\n'
            '⚔️ **الفرق والمتصدرون:** ترتيب الفرق، بدء وإنهاء الحرب ونشر المتصدرين.\n'
            '💰 **البنك والمستويات:** الأرصدة والريالات والخبرة والمستويات والمتجر والرواتب.\n'
            '🛠️ **الحفظ والصيانة:** نسخ احتياطي، التحضير للإغلاق، فحص الألعاب والمناسبات وتصحيح الخبرة.\n\n'
            'تظهر الأدوات المسموحة لك فقط، وصلاحيات القائد والنائب محفوظة.\n'
            'العمليات تعرض تأكيداً قبل التنفيذ؛ الإيمبيد والمتجر يفتحان دليل خيارات أمرهما.\n'
            '**إدارة الرومات:** اكتب آيدي الروم في خانة channel؛ تركها فارغة يستهدف روم اللوحة.'
        ), color=discord.Color.blue()
    )
    embed.set_image(url=BANNER_IMAGE_URL)
    apply_branding(embed, interaction.guild)
    try:
        await target.send(embed=embed, view=AdminControlPanelView())
    except discord.HTTPException:
        await interaction.followup.send('❌ تعذر نشر اللوحة؛ تأكد من صلاحيات البوت في الروم.', ephemeral=True)
        return
    await interaction.followup.send(f'✅ تم إرسال لوحة الإدارة إلى {target.mention}.', ephemeral=True)


# ----------------- أوامر الـ Slash -----------------

# أمر إرسال لوحة التحكم المركزية (Control Panel)
@bot.tree.command(name="control_panel", description="إرسال لوحة التحكم المركزية للأعضاء (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(channel="الروم المراد إرسال اللوحة فيه (اختياري)")
@is_owner_or_deputy()
async def control_panel(interaction: discord.Interaction, channel: discord.TextChannel = None):
    await interaction.response.defer(ephemeral=True)
    target_channel = channel or interaction.channel
    
    embed = discord.Embed(
        title="☀️ MEMBER CONTROL CENTER",
        description=(
            "أهلاً بك! هذي لوحة خدماتك في السيرفر. اقرأ شرح الزر ثم اضغطه بالأسفل.\n\n💬 **الرومات الصوتية المؤقتة**\nيعرض طريقة إنشاء رومك الخاص: تدخل روم إنشاء الفويس، ثم ينقلك البوت لرومك. من لوحة الروم تقدر تغيّر الاسم والعدد والخصوصية وتدير الأعضاء. ينحذف الروم إذا خرج الجميع.\n\n📊 **إحصائيات السيرفر — للإدارة فقط**\nيعرض معلومات السيرفر مثل المالك، عدد الأعضاء، تاريخ الإنشاء ومستوى البوست. متاح للأونر والنائب والرتب الإدارية المعتمدة.\n\n🏆 **صلاحيات ومكافآت اللفلات**\nيشرح مميزات مستويات البرونزي والفضي والذهبي والبلاتيني، والصلاحيات اللي تفتح لك مع تقدمك. لمعرفة مستواك الحالي اضغط «رتبتي (Rank)».\n\n🛠️ **الدعم الفني**\nيفتح قائمة أنواع التذاكر؛ اختر نوع طلبك لفتح تذكرة والتواصل مع الإدارة. إذا عندك تذكرة مفتوحة، يرسل لك رابطها بدل فتح تذكرة ثانية.\n\n🪪 **بروفايلي**\nيعرض معلومات حسابك داخل السيرفر، مثل اللفل والترتيب وتاريخ الانضمام، في بطاقة تظهر لك بشكل خاص.\n\n🏅 **رتبتي (Rank)**\nيعرض صورة فيها مستواك، ترتيبك، خبرتك الحالية والخبرة المطلوبة للمستوى التالي.\n\n❓ **المساعدة**\nيفتح دليل البوت حسب الأقسام. اختر القسم من القائمة لمعرفة أوامره وطريقة استخدامه؛ تظهر خيارات الإدارة للمصرح لهم.\n\n🗳️ **إرسال اقتراح**\nيفتح نافذة تكتب فيها فكرتك ثم ترسلها. يُنشر الاقتراح بروم الاقتراحات للتصويت ومتابعة حالته: قيد الدراسة، مقبول، تم التنفيذ أو مرفوض. تحديثاته الخاصة حسب تفضيلاتك.\n\n💰 **كشف البنك**\nيعرض رصيدك بشكل خاص. من «سجل المعاملات» تشوف التحويلات والمكافآت والمشتريات والخصومات، مع الرصيد بعد كل عملية؛ ومن «تفضيلاتي» تتحكم بالرسائل الخاصة.\n\n💸 **تحويل لشخص**\nاختر المستلم من القائمة، ثم اكتب مبلغ التحويل وأرسله. يُخصم من رصيدك ويُضاف للمستلم؛ لازم يكون عندك رصيد كافٍ.\n\n🧭 **مهامي**\nيعرض مهامك اليومية والأسبوعية وتقدمك فيها. داخله تقدر تبدّل مهمة غير مكتملة مرة باليوم مقابل 25 ريال، وتفتح تقرير هذا الأسبوع أو السابق، وتتحكم بالتفضيلات. المهمة البديلة تبدأ من صفر.\n\n⚔️ **فريقي**\nيعرض فريقك بالجولة الحالية، مجموع نقاطه وترتيبه، ومساهمة كل عضو من الخبرة واللقطات ونسبته من المجموع. اضغط «تحديث» لعرض آخر النقاط. يتطلب وجود حرب نشطة وأن تكون مشاركًا فيها.\n\n🔒 بياناتك الشخصية مثل الرصيد والمهام والفريق تظهر لك بشكل خاص. الاقتراح يُنشر في رومه، والتذكرة يطلع عليها المخوّلون.\nاضغط الزر المطلوب بالأسفل.\n\n🎮 **اختيار الرانك**\nيفتح لوحة رتب روكت ليق بشكل خاص. اختر رانكك ليعطيك البوت رتبته بدل القديمة؛ القراند I وII وIII متاحة، والسوبر سونيك يكون بالتنسيق مع الإدارة."
        ),
        color=discord.Color.from_rgb(0, 168, 252)
    )
    embed.set_image(url=BANNER_IMAGE_URL)
    apply_branding(embed, interaction.guild)
    
    await target_channel.send(embed=embed, view=ControlPanelView())
    await interaction.followup.send(f"✅ تم إرسال بانل لوحة التحكم بنجاح إلى {target_channel.mention}!", ephemeral=True)

# أمر المساعدة التفاعلي (متاح للجميع)
@bot.tree.command(name="help", description="عرض قائمة تفاعلية بكل أوامر البوت مقسّمة حسب القسم")
async def help_command(interaction: discord.Interaction):
    is_staff = is_staff_member(interaction.user)
    default_topic = HELP_TOPICS["general"]
    embed = discord.Embed(title=default_topic["title"], description=default_topic["text"], color=default_topic["color"])
    embed.set_image(url=BANNER_IMAGE_URL)
    apply_branding(embed, interaction.guild, user=interaction.user)
    await interaction.response.send_message(embed=embed, view=HelpView(is_staff), ephemeral=True)

# أمر إرسال اقتراح جديد (للأونر والنائب فقط عبر السلاش كوماند — بقية الأعضاء يستخدمون زر "إرسال اقتراح" بلوحة التحكم)
@bot.tree.command(name="suggest", description="إرسال اقتراح جديد للإدارة عبر نافذة كتابة سريعة (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@is_owner_or_deputy()
async def suggest_command(interaction: discord.Interaction):
    await interaction.response.send_modal(SuggestionModal())

# أمر إرسال لوحة تحكم القائد والنائب بالرومات الصوتية الثابتة المحددة
@bot.tree.command(name="setup_leader_voice_panel", description="إرسال لوحة تحكم صوتية للقائد والنائب بالرومات الصوتية الثابتة المحددة مسبقاً (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@is_owner_or_deputy()
async def setup_leader_voice_panel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    sent_channels = []
    failed_channels = []

    for cid in LEADER_DEPUTY_VOICE_PANEL_CHANNEL_IDS:
        channel = guild.get_channel(cid)
        if channel is None:
            failed_channels.append(str(cid))
            continue
        try:
            embed = discord.Embed(
                title="👑 واجهة التحكم الصوتية — القائد والنائب",
                description=(
                    "يمكن استخدام هذه **الواجهة** للتحكم بهذه القناة الصوتية.\n"
                    "**هذه الأزرار مخصصة لصاحب السيرفر (القائد) ونائب الإدارة فقط.**\n\n"
                    "**اضغط الأزرار أدناه لاستخدام الواجهة، أو زر ❓ مساعدة لشرح تفصيلي بالصور:**"
                ),
                color=discord.Color.from_rgb(0, 168, 252)
            )
            apply_branding(embed, guild)

            # نفس صورة الـ GIF التزيينية المستخدمة فوق لوحة الفويس المؤقت
            decor_embed = discord.Embed(color=discord.Color.from_rgb(0, 168, 252))
            decor_embed.set_image(url="https://i.imgur.com/pgi8UCX.gif")

            view = LeaderDeputyVoiceControlView(channel_id=cid)
            view.base_embed = embed
            view.decor_embed = decor_embed

            sent_msg = await channel.send(embeds=[decor_embed, embed], view=view)
            view.status_message = sent_msg
            try:
                await sent_msg.pin(reason="تثبيت لوحة تحكم القائد والنائب")
            except discord.Forbidden:
                print(f"[setup_leader_voice_panel] ما يملك البوت صلاحية تثبيت الرسالة بروم {cid}")
            except Exception as e:
                print(f"[setup_leader_voice_panel] تعذّر تثبيت اللوحة بروم {cid}: {e}")
            sent_channels.append(channel.mention)
        except Exception as e:
            print(f"[setup_leader_voice_panel] خطأ أثناء الإرسال لروم {cid}: {e}")
            failed_channels.append(str(cid))

    result_msg = ""
    if sent_channels:
        result_msg += f"✅ تم إرسال اللوحة بنجاح إلى: {', '.join(sent_channels)}\n"
    if failed_channels:
        result_msg += f"❌ تعذر الإرسال للقنوات التالية (تأكد من الآيدي): {', '.join(failed_channels)}"
    await interaction.followup.send(result_msg or "❌ لم يتم إرسال أي لوحة.", ephemeral=True)

# أمر قفل الروم (/lock)
@bot.tree.command(name="lock", description="قفل الروم الحالي ومنع الأعضاء من الكتابة (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(channel="الروم المطلوب قفله (اختياري، الافتراضي الروم الحالي)")
@is_owner_or_deputy()
async def lock(interaction: discord.Interaction, channel: discord.TextChannel = None):
    await interaction.response.defer(ephemeral=True)
    channel = channel or interaction.channel
    guild = interaction.guild
    try:
        overwrite = channel.overwrites_for(guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(guild.default_role, overwrite=overwrite)
        await interaction.followup.send("🔒 تم قفل الروم بنجاح.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ حدث خطأ: {e}", ephemeral=True)

# أمر فتح الروم (/unlock)
@bot.tree.command(name="unlock", description="فتح الروم الحالي والسماح للأعضاء بالكتابة (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(channel="الروم المطلوب فتحه (اختياري، الافتراضي الروم الحالي)")
@is_owner_or_deputy()
async def unlock(interaction: discord.Interaction, channel: discord.TextChannel = None):
    await interaction.response.defer(ephemeral=True)
    channel = channel or interaction.channel
    guild = interaction.guild
    try:
        overwrite = channel.overwrites_for(guild.default_role)
        overwrite.send_messages = True
        await channel.set_permissions(guild.default_role, overwrite=overwrite)
        await interaction.followup.send("🔓 تم فتح الروم بنجاح.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ حدث خطأ: {e}", ephemeral=True)

# أمر إرسال رسالة Embed مخصصة (/embed)
@bot.tree.command(name="embed", description="إرسال رسالة Embed مخصصة بالكامل: عنوان، صور، فوتر، author وأكثر (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    title="عنوان الرسالة (اختياري)",
    description="محتوى ووصف الرسالة (اختياري)",
    color="لون الـ Embed بصيغة hex مثل #00a8fc (اختياري، افتراضي أزرق)",
    url="رابط يخلي العنوان قابل للنقر (اختياري)",
    image_url="رابط صورة كبيرة تظهر أسفل الإيمبيد (اختياري)",
    image="ارفع صورة من جهازك داخل الإيمبيد؛ لها الأولوية على رابط الصورة (اختياري)",
    thumbnail_url="رابط صورة صغيرة تظهر أعلى يمين الإيمبيد (اختياري)",
    author_name="اسم يظهر بأعلى الإيمبيد فوق العنوان (اختياري)",
    author_icon_url="أيقونة صغيرة جنب اسم الـ author (اختياري)",
    footer_text="نص يظهر بأسفل الإيمبيد (اختياري)",
    footer_icon_url="أيقونة صغيرة جنب نص الفوتر (اختياري)",
    show_timestamp="إضافة توقيت الإرسال الحالي بالفوتر (اختياري، الافتراضي لا)",
    channel="الروم المطلوب الإرسال فيه (اختياري، الافتراضي هذا الروم)"
)
@is_owner_or_deputy()
async def embed_msg(
    interaction: discord.Interaction,
    title: str = None,
    description: str = None,
    color: str = None,
    url: str = None,
    image_url: str = None,
    thumbnail_url: str = None,
    author_name: str = None,
    author_icon_url: str = None,
    footer_text: str = None,
    footer_icon_url: str = None,
    show_timestamp: bool = False,
    channel: discord.TextChannel = None,
    image: discord.Attachment = None
):
    await interaction.response.defer(ephemeral=True)

    if not any([title, description, image, image_url, thumbnail_url, author_name, footer_text]):
        await interaction.followup.send("❌ لازم تحدد شي واحد على الأقل (عنوان، وصف، صورة، author، أو فوتر).", ephemeral=True)
        return

    if image:
        image_extension = os.path.splitext(image.filename)[1].lower()
        if image_extension not in {".png", ".jpg", ".jpeg", ".gif", ".webp"} or (
            image.content_type and not image.content_type.startswith("image/")
        ):
            await interaction.followup.send("❌ ارفع صورة بصيغة PNG أو JPG أو GIF أو WEBP.", ephemeral=True)
            return
        upload_limit = interaction.guild.filesize_limit if interaction.guild else 10 * 1024 * 1024
        if image.size > upload_limit:
            await interaction.followup.send("❌ حجم الصورة أكبر من الحد المسموح في السيرفر؛ ارفع صورة أصغر.", ephemeral=True)
            return

    embed_color = discord.Color.blue()
    if color:
        try:
            clean_color = color.strip("#")
            embed_color = discord.Color(int(clean_color, 16))
        except Exception:
            await interaction.followup.send("❌ صيغة اللون غير صحيحة، استخدم hex مثل #00a8fc.", ephemeral=True)
            return

    embed = discord.Embed(color=embed_color)
    if title:
        embed.title = title
    if description:
        embed.description = description
    if url and title:
        embed.url = url
    if image_url:
        embed.set_image(url=image_url)
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)
    if author_name:
        embed.set_author(name=author_name, icon_url=author_icon_url)
    if footer_text:
        embed.set_footer(text=footer_text, icon_url=footer_icon_url)
    if show_timestamp:
        embed.timestamp = discord.utils.utcnow()

    target_channel = channel or interaction.channel

    try:
        if image:
            image_file = await image.to_file(filename=f"embed_image{image_extension}")
            try:
                embed.set_image(url=f"attachment://{image_file.filename}")
                await target_channel.send(embed=embed, file=image_file)
            finally:
                image_file.close()
        else:
            await target_channel.send(embed=embed)
    except discord.Forbidden:
        await interaction.followup.send("❌ ما يملك البوت صلاحية الإرسال بهذا الروم.", ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(f"❌ حدث خطأ: {e}", ephemeral=True)
        return

    await interaction.followup.send(f"✅ تم إرسال رسالة الـ Embed بنجاح إلى {target_channel.mention}!", ephemeral=True)

# ----------------- أوامر متجر العملة -----------------
@bot.tree.command(name="shop", description="إرسال رسالة متجر بعروضك (صورة + اسم لكل عنصر) مع زر شراء أخضر (لصاحب السيرفر فقط)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    channel="الروم اللي بينزل فيه المتجر",
    title="عنوان ترحيبي أعلى المتجر (اختياري)",
    description="وصف عام يظهر أعلى العروض (اختياري)",
    item1_name="اسم العنصر الأول",
    item1_price=f"سعر العنصر الأول بـ{CURRENCY_NAME}",
    item1_image="رابط صورة العنصر الأول (اختياري)",
    item2_name="اسم العنصر الثاني (اختياري)",
    item2_price=f"سعر العنصر الثاني بـ{CURRENCY_NAME} (اختياري)",
    item2_image="رابط صورة العنصر الثاني (اختياري)",
    item3_name="اسم العنصر الثالث (اختياري)",
    item3_price=f"سعر العنصر الثالث بـ{CURRENCY_NAME} (اختياري)",
    item3_image="رابط صورة العنصر الثالث (اختياري)",
    item4_name="اسم العنصر الرابع (اختياري)",
    item4_price=f"سعر العنصر الرابع بـ{CURRENCY_NAME} (اختياري)",
    item4_image="رابط صورة العنصر الرابع (اختياري)",
    item5_name="اسم العنصر الخامس (اختياري)",
    item5_price=f"سعر العنصر الخامس بـ{CURRENCY_NAME} (اختياري)",
    item5_image="رابط صورة العنصر الخامس (اختياري)"
)
@is_owner_only()
async def shop_cmd(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str = None,
    description: str = None,
    item1_name: str = None,
    item1_price: int = None,
    item1_image: str = None,
    item2_name: str = None,
    item2_price: int = None,
    item2_image: str = None,
    item3_name: str = None,
    item3_price: int = None,
    item3_image: str = None,
    item4_name: str = None,
    item4_price: int = None,
    item4_image: str = None,
    item5_name: str = None,
    item5_price: int = None,
    item5_image: str = None,
):
    await interaction.response.defer(ephemeral=True)

    raw_items = [
        (item1_name, item1_price, item1_image),
        (item2_name, item2_price, item2_image),
        (item3_name, item3_price, item3_image),
        (item4_name, item4_price, item4_image),
        (item5_name, item5_price, item5_image),
    ]

    # نتحقق من كل خانة فيها اسم أو سعر تحديد صحيح (لازم الاثنين مع بعض)
    valid_items = []
    for idx, (name, price, image) in enumerate(raw_items, start=1):
        if name is None and price is None:
            continue
        if name is None or price is None:
            await interaction.followup.send(
                f"❌ العنصر رقم {idx}: لازم تحدد الاسم والسعر مع بعض (ما يصلح توفر واحد بدون الثاني).",
                ephemeral=True
            )
            return
        if price <= 0:
            await interaction.followup.send(f"❌ سعر العنصر **{name}** لازم يكون أكبر من صفر.", ephemeral=True)
            return
        valid_items.append((name, price, image))

    if not valid_items:
        await interaction.followup.send("❌ لازم تحدد عنصر واحد على الأقل (اسم + سعر).", ephemeral=True)
        return

    # نسجل العناصر بقاعدة البيانات عشان أزرار الشراء تشتغل بشكل دائم حتى بعد إعادة تشغيل البوت
    created_items = []
    for name, price, image in valid_items:
        item_id = await add_shop_item(interaction.guild.id, name, price, image)
        created_items.append((item_id, name, price, image))

    embeds = []
    if title or description:
        intro_embed = discord.Embed(
            title=title or "🛒 المتجر",
            description=description,
            color=discord.Color.gold()
        )
        apply_branding(intro_embed, interaction.guild)
        embeds.append(intro_embed)

    for item_id, name, price, image in created_items:
        item_embed = discord.Embed(
            title=f"🛍️ {name}",
            description=f"💰 السعر: `{price:,} {CURRENCY_NAME}` {CURRENCY_EMOJI}",
            color=discord.Color.from_rgb(0, 168, 252)
        )
        if image:
            item_embed.set_image(url=image)
        embeds.append(item_embed)

    view = build_shop_view(created_items)

    try:
        await channel.send(embeds=embeds, view=view)
    except discord.Forbidden:
        await interaction.followup.send("❌ ما يملك البوت صلاحية الإرسال بهذا الروم.", ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(f"❌ حدث خطأ أثناء إرسال المتجر: {e}", ephemeral=True)
        return

    await interaction.followup.send(
        f"✅ تم إرسال المتجر بنجاح إلى {channel.mention} بـ **{len(created_items)}** عنصر.\n"
        f"ℹ️ تقدر تشغل الأمر مرة ثانية لإضافة عروض جديدة برسالة منفصلة.",
        ephemeral=True
    )

@bot.tree.command(name="shop_remove_item", description="إيقاف عنصر من المتجر عشان زر شرائه ما يشتغل بعدها (لصاحب السيرفر فقط)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(item_id="رقم العنصر (id) — يظهر لك بروم السجلات وقت ما تسوي عملية شراء، أو تقدر تحسبه بترتيب إنشاء العناصر")
@is_owner_only()
async def shop_remove_item_cmd(interaction: discord.Interaction, item_id: int):
    await interaction.response.defer(ephemeral=True)
    item = await get_shop_item(item_id)
    if not item or item["guild_id"] != interaction.guild.id:
        await interaction.followup.send("❌ ما فيه عنصر بهذا الرقم بهذا السيرفر.", ephemeral=True)
        return
    if not item["active"]:
        await interaction.followup.send(f"ℹ️ العنصر **{item['name']}** متوقف مسبقاً.", ephemeral=True)
        return
    await deactivate_shop_item(item_id)
    await interaction.followup.send(f"✅ تم إيقاف العنصر **{item['name']}** — زر شرائه بالرسائل القديمة ما راح يشتغل بعد الآن.", ephemeral=True)

# ----------------- إعلان الميزات الجديدة (المتجر، الليدربورد، الروليت، الرواتب، البنك، التحويل) -----------------
@bot.tree.command(name="announce_features", description="إرسال إيمبيد يعرّف الأعضاء بالميزات الجديدة مع منشن الجميع (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(channel="الروم اللي بينزل فيه الإعلان")
@is_owner_or_deputy()
async def announce_features_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)

    embed = discord.Embed(
        title="✨ ميزات جديدة بالسيرفر!",
        description=(
            "أضفنا مجموعة أنظمة جديدة كلياً تفيدكم وتضيف حماس أكثر بالسيرفر، "
            "تعرّفوا عليها بالأسفل 👇"
        ),
        color=discord.Color.from_rgb(0, 168, 252)
    )
    embed.set_image(url="https://i.imgur.com/pgi8UCX.gif")

    embed.add_field(
        name="⚔️ تمت إضافة حروب الفرق!",
        value=(
            f"**التوزيع:** الأعضاء المعتمدون يتوزعون عشوائيًا على فرق، كل فريق فيه {WAR_TEAM_SIZE} أعضاء.\n"
            "**القائد:** يُختار من قائمة القادة المعتمدين، ويسمّي فريقه مرة واحدة بالجولة.\n"
            "**جمع النقاط:** خبرة نشاطك تُحسب لفريقك، مع استمرار احتسابها للفلك الشخصي.\n"
            f"**اللقطات:** كل لقطة مؤهلة تضيف {WAR_CLIP_POINTS} نقاط للفريق، بحد {WAR_CLIP_DAILY_CAP} لقطات للعضو يوميًا.\n"
            "**المتابعة:** افتح «فريقي» في الكنترول بنل لعرض ترتيب فريقك ونقاطه ومساهمة الأعضاء؛ "
            "واستخدم `/houses` لترتيب جميع الفرق.\n"
            f"**الجوائز:** المراكز الثلاثة الأولى تحصل على {WAR_PRIZES[0]} / {WAR_PRIZES[1]} / {WAR_PRIZES[2]} {CURRENCY_NAME} "
            + ("لكل عضو بالفريق" if WAR_PRIZE_PER_MEMBER else "للفريق وتُقسّم بين أعضائه")
            + "، حسب ترتيب الجولة. التوزيع والنتائج تُنشر في الإعلانات."
        ),
        inline=False
    )

    embed.add_field(
        name="🛒 المتجر",
        value=(
            f"تقدر تشتري عروض وخدمات مقابل {CURRENCY_NAME}اتك عبر زر شراء أخضر، "
            "وبعد ما تأكد عملية الشراء تنفتح لك تذكرة تلقائية برقم طلبك."
        ),
        inline=False
    )
    embed.add_field(
        name="🏆 لوحة المتصدرين",
        value="فيه نشر أسبوعي تلقائي لأبرز المتصدرين بالسيرفر، تابعوا الإعلانات!",
        inline=False
    )
    embed.add_field(
        name="🎡 الروليت اليومي",
        value=f"استخدم زر لوحة الروليت مرة كل يوم وجرب حظك تربح من 5 إلى 50 {CURRENCY_NAME}!",
        inline=False
    )
    embed.add_field(
        name="💰 الرواتب الشهرية",
        value="أصحاب الرتب المميزة يستلمون راتبهم الشهري تلقائياً آخر يوم بكل شهر بدون أي طلب.",
        inline=False
    )
    embed.add_field(
        name="🏦 البنك",
        value=f"استخدم `/balance` عشان تشوف رصيدك الحالي من {CURRENCY_NAME} بأي وقت.",
        inline=False
    )
    embed.add_field(
        name="💸 التحويل بين الأعضاء",
        value="تقدر تحول من رصيدك لأي عضو ثاني مباشرة عبر زر «تحويل لشخص» بلوحة التحكم الصوتية.",
        inline=False
    )

    embed.set_footer(text="B4E", icon_url="https://i.imgur.com/KRElNgr.gif")

    try:
        await channel.send(
            content="@everyone",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=True)
        )
    except discord.Forbidden:
        await interaction.followup.send("❌ ما يملك البوت صلاحية الإرسال أو المنشن بهذا الروم.", ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(f"❌ حدث خطأ أثناء إرسال الإعلان: {e}", ephemeral=True)
        return

    await interaction.followup.send(f"✅ تم إرسال إعلان الميزات الجديدة بنجاح إلى {channel.mention}.", ephemeral=True)

# أمر مسح وحذف الرسائل الشامل (/clear)
@bot.tree.command(name="clear", description="حذف الرسائل: عدد محدد، كل الرسائل، أو رسائل شخص معين (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    amount="عدد الرسائل المراد حذفها (افتراضياً 10)",
    user="حذف رسائل شخص معين فقط (اختياري)",
    mode="اختر الوضع: عدد محدد، كل الرسائل، أو رسائل شخص معين",
    channel="الروم المطلوب تنظيفه (اختياري، الافتراضي الروم الحالي)"
)
@app_commands.choices(mode=[
    app_commands.Choice(name="عدد محدد (Amount)", value="amount"),
    app_commands.Choice(name="كل الرسائل (All Messages)", value="all"),
    app_commands.Choice(name="رسائل شخص معين (From User)", value="user")
])
@is_owner_or_deputy()
async def clear(interaction: discord.Interaction, mode: str = "amount", amount: int = 10, user: discord.Member = None, channel: discord.TextChannel = None):
    await interaction.response.defer(ephemeral=True)
    channel = channel or interaction.channel
    log_channel = bot.get_channel(LOG_CHANNEL_ID)

    try:
        if mode == "amount":
            if amount <= 0:
                await interaction.followup.send("❌ يرجى إدخال عدد أكبر من صفر.", ephemeral=True)
                return
            deleted = await channel.purge(limit=amount)
            await interaction.followup.send(f"✅ تم حذف **{len(deleted)}** رسالة بنجاح.", ephemeral=True)

            if log_channel:
                embed = discord.Embed(title="🧹 حذف جماعي للرسائل (Clear)", color=discord.Color.dark_grey())
                embed.add_field(name="🛡️ الإداري", value=interaction.user.mention, inline=True)
                embed.add_field(name="📍 الروم", value=channel.mention, inline=True)
                embed.add_field(name="🔢 عدد الرسائل المحذوفة", value=str(len(deleted)), inline=True)
                apply_log_branding(embed, interaction.guild, target=interaction.user)
                await log_channel.send(embed=embed)

        elif mode == "all":
            pos = channel.position
            if log_channel and log_channel.id != channel.id:
                embed = discord.Embed(title="🧹 تنظيف كامل لقناة (إعادة إنشاء)", color=discord.Color.dark_grey())
                embed.add_field(name="🛡️ الإداري", value=interaction.user.mention, inline=True)
                embed.add_field(name="📍 الروم", value=f"`#{channel.name}`", inline=True)
                apply_log_branding(embed, interaction.guild, target=interaction.user)
                await log_channel.send(embed=embed)
            new_channel = await channel.clone(reason=f"Clear all messages by {interaction.user.name}")
            await channel.delete(reason="Clear all messages purge")
            await new_channel.edit(position=pos)
            await new_channel.send(f"🧹 تم تنظيف وحذف جميع رسائل الروم بواسطة {interaction.user.mention}.", delete_after=5)

        elif mode == "user":
            if not user:
                await interaction.followup.send("❌ يجب تحديد العضو المراد حذف رسائله.", ephemeral=True)
                return
            
            deleted = await channel.purge(limit=100, check=lambda m: m.author.id == user.id)
            await interaction.followup.send(f"✅ تم حذف **{len(deleted)}** رسالة تخص العضو {user.mention}.", ephemeral=True)

            if log_channel:
                embed = discord.Embed(title="🧹 حذف رسائل عضو معيّن (Clear)", color=discord.Color.dark_grey())
                embed.add_field(name="🛡️ الإداري", value=interaction.user.mention, inline=True)
                embed.add_field(name="👤 العضو المستهدف", value=user.mention, inline=True)
                embed.add_field(name="📍 الروم", value=channel.mention, inline=True)
                embed.add_field(name="🔢 عدد الرسائل المحذوفة", value=str(len(deleted)), inline=True)
                apply_log_branding(embed, interaction.guild, target=user)
                await log_channel.send(embed=embed)

    except discord.Forbidden:
        await interaction.followup.send("❌ لا يملك البوت صلاحية إدارة الرسائل أو حذفها في هذه الروم!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ حدث خطأ أثناء الحذف: {e}", ephemeral=True)

async def send_profile_embed(interaction: discord.Interaction, target: discord.Member):
    guild = interaction.guild
    xp, level, _daily_v_xp = await get_user_data(guild.id, target.id)
    needed = get_xp_needed(level)
    rank_pos = await get_server_rank(guild.id, target.id)
    birthday = await get_member_birthday(guild.id, target.id)
    rl_rank = get_member_rl_rank_label(target)

    if target.id == guild.owner_id:
        status_text = "👑 صاحب السيرفر"
    elif is_staff_member(target):
        status_text = "🛡️ طاقم الإدارة"
    else:
        status_text = "👤 عضو"

    xp_line = f"المستوى **{level}** — `{xp}/{needed if needed else xp}` XP" if needed else f"المستوى **{level}** (أقصى مستوى) 🏆"

    embed = discord.Embed(
        title=f"🪪 بروفايل {target.display_name}",
        color=discord.Color.from_rgb(0, 168, 252)
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_image(url=BANNER_IMAGE_URL)
    embed.add_field(name="🛡️ الحالة", value=status_text, inline=True)
    embed.add_field(name="🏆 اللفل والخبرة", value=xp_line, inline=True)
    embed.add_field(name="📊 الترتيب", value=f"#{rank_pos}", inline=True)
    embed.add_field(name="📅 تاريخ الانضمام", value=f"<t:{int(target.joined_at.timestamp())}:D>" if target.joined_at else "غير معروف", inline=True)
    embed.add_field(name="🗓️ تاريخ إنشاء الحساب", value=f"<t:{int(target.created_at.timestamp())}:D>", inline=True)
    embed.add_field(name="🎂 عيد الميلاد", value=(f"{birthday[1]}/{birthday[0]}" if birthday else "غير مسجل بعد"), inline=True)
    embed.add_field(name="🎮 رتبة روكت ليق", value=(rl_rank if rl_rank else "غير مسجلة بعد"), inline=True)
    apply_branding(embed, guild, user=target)

    await interaction.followup.send(embed=embed, ephemeral=True)

# 0. أمر عرض البروفايل الشخصي (متاح للجميع)
@bot.tree.command(name="profile", description="عرض بروفايلك أو بروفايل عضو آخر: اللفل، الترتيب، تاريخ الانضمام وأكثر")
@app_commands.describe(member="العضو المطلوب عرض بروفايله (اختياري، اتركه فاضي لعرض بروفايلك)")
async def profile(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer(ephemeral=True)
    target = member or interaction.user
    await send_profile_embed(interaction, target)

# 1. أمر عرض الرتبة القديم كصورة (متاح للجميع)
@bot.tree.command(name="rank", description="عرض بطاقة مستواك ونقاط خبرتك الحالية كصورة")
async def rank(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer()
    target = member or interaction.user
    xp, level, daily_v_xp = await get_user_data(interaction.guild.id, target.id)
    needed = get_xp_needed(level)
    rank_pos = await get_server_rank(interaction.guild.id, target.id)

    image_buffer = await create_rank_card(target, level, xp, needed, rank_pos, daily_v_xp)
    discord_file = discord.File(fp=image_buffer, filename="rank.png")
    await interaction.followup.send(file=discord_file)

# 2. أمر لوحة المتصدرين كصورة مصممة (متاح للجميع)
@bot.tree.command(name="leaderboard", description="عرض لوحة المتصدرين في السيرفر كصورة مصممة (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@is_owner_or_deputy()
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    
    image_buffer = await generate_leaderboard_image(interaction.guild)
    if not image_buffer:
        await interaction.followup.send("❌ لا توجد بيانات تلفيل مسجلة في السيرفر حتى الآن.", ephemeral=True)
        return

    discord_file = discord.File(fp=image_buffer, filename="leaderboard.png")
    await interaction.followup.send(file=discord_file)

# 3. أمر نشر الليدربورد الأسبوعي فوراً (للأونر والنائب) — يسوي بالضبط نفس اللي تسويه المهمة التلقائية كل جمعة
@bot.tree.command(name="post_weekly_leaderboard", description="إرسال لوحة المتصدرين الأسبوعية فوراً بنفس طريقة النشر التلقائي (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@is_owner_only()
async def post_weekly_leaderboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    channel = bot.get_channel(WEEKLY_LEADERBOARD_CHANNEL_ID)
    if not channel:
        await interaction.followup.send("❌ لم يتم العثور على الروم، تأكد من WEEKLY_LEADERBOARD_CHANNEL_ID.", ephemeral=True)
        return

    guild = interaction.guild
    image_buffer = await generate_weekly_leaderboard_image(guild)
    if not image_buffer:
        await interaction.followup.send("❌ ما فيه أي عضو اكتسب XP هذا الأسبوع لحد الآن.", ephemeral=True)
        return

    await update_weekly_top_role(guild)

    discord_file = discord.File(fp=image_buffer, filename="weekly_leaderboard.png")
    msg_content = (
        f"@everyone\n"
        f"🏆 **لوحة المتصدرين الأسبوعية | {guild.name}** 🏆\n"
        f"تهانينا للمتصدرين هذا الأسبوع! استمروا في التفاعل والنشاط الصوتي والكتابي لتصدر القائمة القادمة 🌟🔥"
    )
    await channel.send(content=msg_content, file=discord_file)

    await reset_weekly_xp(guild.id)

    await interaction.followup.send(f"✅ تم إرسال لوحة المتصدرين بنجاح إلى {channel.mention}.", ephemeral=True)

# 3.4 أمر نشر ليدربورد أغنى الأعضاء فوراً (للأونر والنائب) — يسوي بالضبط نفس اللي تسويه المهمة التلقائية كل 3 أيام
@bot.tree.command(name="post_money_leaderboard", description="إرسال قائمة أغنى الأعضاء فوراً بنفس طريقة النشر التلقائي (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@is_owner_only()
async def post_money_leaderboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    channel = bot.get_channel(MONEY_LEADERBOARD_CHANNEL_ID)
    if not channel:
        await interaction.followup.send("❌ لم يتم العثور على الروم، تأكد من MONEY_LEADERBOARD_CHANNEL_ID.", ephemeral=True)
        return

    guild = interaction.guild
    image_buffer = await generate_money_leaderboard_image(guild)
    if not image_buffer:
        await interaction.followup.send("❌ ما فيه أي عضو عنده رصيد بالبنك حالياً.", ephemeral=True)
        return

    discord_file = discord.File(fp=image_buffer, filename="money_leaderboard.png")
    msg_content = (
        f"@everyone\n"
        f"💰 **قائمة أغنى الأعضاء | {guild.name}** 💰\n"
        f"هذي قائمة بأصحاب أعلى رصيد بالبنك حالياً، تُحدَّث كل 3 أيام تلقائياً!"
    )
    await channel.send(content=msg_content, file=discord_file, allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False))
    await asyncio.to_thread(set_bot_state, "money_leaderboard_last_post", str(time.time()))
    await interaction.followup.send(f"✅ تم إرسال قائمة أغنى الأعضاء بنجاح إلى {channel.mention}.", ephemeral=True)

# 3.3.1 أمر عرض قوانين Steam Family
STEAM_FAMILY_BANK_ACCOUNTS = [
    ("سليمان", "564000010006086071639", None),
    ("محمد", "101000010006086166942", None),
    ("عبدالإله", "101000010006086166553", None),
    ("عبدالمحسن", "SA7130100991102950736012", "بنك العربي"),
    ("عبدالرحمن", "—", None),
]

def build_steam_family_rules_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🎮 قوانين Steam Family",
        description=(
            "⚠️ **تنبيه مهم**\n"
            "دخولك إلى Steam Family يعني أنك موافق على جميع القوانين.\n"
            "**في حال عدم قراءة القوانين، فنحن غير مسؤولين عن أي مخالفة أو عقوبة تحصل بسبب عدم معرفتك بالنظام.**"
        ),
        color=discord.Color.from_rgb(0, 168, 252)
    )
    embed.add_field(
        name="1️⃣ نظام شراء الألعاب",
        value=(
            "• أي لعبة جديدة تُشترى من حساب الشخص اللي وجدها أو اقترحها.\n"
            "• قيمتها تُقسّم بالتساوي بين كل الراغبين باستخدامها.\n"
            "• الشخص اللي وجد اللعبة مسؤول عن الشراء من حسابه فقط، وما يعني إنه يتحمل قيمتها كاملة."
        ),
        inline=False
    )
    embed.add_field(
        name="2️⃣ نظام الدفع والاستخدام",
        value=(
            "• ممنوع اللعب قبل دفع الحصة كاملة، وما فيه تأجيل دفع ثم لعب لاحقاً.\n"
            "• بظروف خاصة يُسمح باللعب فقط بموافقة 3 أعضاء على الأقل، أو بدفع شخص ثاني حصته عنه.\n"
            "• الموافقة على اللعب لا تعفي من دفع الحصة، وتبقى مستحقة عليه.\n"
            "• أي لعب للعبة (جماعي أو فردي) يتطلب المشاركة بدفع قيمتها."
        ),
        inline=False
    )
    embed.add_field(
        name="3️⃣ نظام الانسحاب",
        value="بعد الاتفاق على الشراء والتقسيم، ما فيه انسحاب — الملتزم يدفع حصته حتى لو قرر لاحقاً ما يلعب.",
        inline=False
    )
    embed.add_field(
        name="4️⃣ العقوبات عند المخالفة",
        value=(
            "من يُكتشف يلعب بدون دفع أو موافقة:\n"
            "• إنذار لمدة أسبوع.\n"
            "• منع من لعب أي لعبة بالفاميلي لمدة أسبوع، حتى اللي دفع حصته فيها.\n"
            "• خصم `100` ريال من رصيد بنك الديسكورد.\n"
            "• إضافة رتبة **Ban** لملفه بالديسكورد."
        ),
        inline=False
    )
    embed.add_field(
        name="5️⃣ التبليغ والمكافآت",
        value="أي شخص يكتشف مخالفة يبلّغ القائد أو النائب فوراً، وسيُتّخذ الإجراء اللازم — ويحصل المُبلّغ على مكافأة تقديرية.",
        inline=False
    )
    embed.add_field(
        name="6️⃣ الشراء الجماعي",
        value="لو مجموعة اشترت لعبة وباقي المجموعات ما تبي تشارك، ما يسمح لهم بالاستخدام إلا بعد دفع حصتهم والانضمام للمجموعة.",
        inline=False
    )
    embed.add_field(
        name="7️⃣ حماية الحسابات",
        value="يمنع منعاً باتاً مشاركة بيانات الحساب أو كلمة المرور مع أي شخص خارج Steam Family — والمسؤولية على صاحب الحساب.",
        inline=False
    )
    accounts_text = "\n".join(
        f"**{name}**{f' ({bank})' if bank else ''}\n`{acc}`"
        for name, acc, bank in STEAM_FAMILY_BANK_ACCOUNTS
    )
    embed.add_field(name="8️⃣ الحسابات البنكية للتحويل (بنك الراجحي)", value=accounts_text, inline=False)
    embed.add_field(
        name="9️⃣ الهدف من القوانين",
        value="تحقيق العدل بين الأعضاء وحفظ حقوق من يدفع ويشارك بشراء الألعاب. عدم قراءة القوانين لا يعفي من العقوبات.",
        inline=False
    )
    embed.set_image(url=BANNER_IMAGE_URL)
    apply_branding(embed, guild)
    return embed

# 3.3.2 أمر عرض القوانين الأساسية بالسيرفر (يشمل الأنظمة والميزات الحالية + سياسة التحذيرات)
def build_server_rules_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="📜 القوانين الأساسية بالسيرفر",
        description=(
            "دخولك للسيرفر يعني موافقتك على كل القوانين بالأسفل. اقرأها بعناية — "
            "**عدم العلم بالقانون لا يعفي من العقوبة.**"
        ),
        color=discord.Color.from_rgb(0, 168, 252)
    )
    embed.add_field(
        name="1️⃣ الاحترام العام",
        value=(
            "• يُمنع السب والقذف والتنمر والعنصرية والتحرش بأي شكل.\n"
            "• يُمنع التمييز الطائفي أو الديني أو العرقي.\n"
            "• يُمنع نشر محتوى إباحي أو صادم أو غير لائق (نصاً أو صوراً أو روابط)."
        ),
        inline=False
    )
    embed.add_field(
        name="2️⃣ السبام والإعلانات",
        value=(
            "• يُمنع السبام بالكتابة أو الصوت أو الإيموجي أو المنشن الجماعي.\n"
            "• يُمنع الإعلان عن سيرفرات أو منتجات أو خدمات خارجية بدون إذن الإدارة.\n"
            "• يُمنع إرسال روابط دعوة أو روابط مشبوهة (فيشنق) بأي روم."
        ),
        inline=False
    )
    embed.add_field(
        name="3️⃣ الرومات والاستخدام الصحيح",
        value=(
            "• استخدم كل روم بالغرض المخصص له (اللقطات، الاقتراحات، الدعم الفني... إلخ).\n"
            "• الرومات الصوتية المؤقتة تُدار من صاحبها عبر لوحة التحكم، ويُمنع إزعاج أو اقتحام رومات الغير.\n"
            "• عند مناداة القائد بروم صوتي (`/summon`) أو أثناء **الخطاب الملكي** (`/royal_address`)، يُمنع التحدث فوق القائد."
        ),
        inline=False
    )
    embed.add_field(
        name="4️⃣ نظام المستويات والاقتصاد",
        value=(
            f"• تكسب XP بالكتابة والفويس، وتُصرف مكافآت مالية `{CURRENCY_NAME}` {CURRENCY_EMOJI} عند كل ترقية مستوى.\n"
            f"• رفع اللقطات (فيديو/صورة) بروم اللقطات المخصص يمنحك مكافأة مالية تلقائية + دخول مسابقة **أفضل لقطة أسبوعية**.\n"
            "• يُمنع استغلال ثغرات النظام (تكرار حسابات، فارمنق XP بطرق غير طبيعية، تصويت وهمي على اللقطات) — "
            "وأي استغلال مثبت يُعرّضك للحظر من الأنظمة المالية بالكامل."
        ),
        inline=False
    )
    embed.add_field(
        name="5️⃣ حرب البيوت الملكية الأسبوعية",
        value=(
            "• كل أسبوع يُوزَّع الأعضاء المحددون للمشاركة عشوائياً على بيوت (3 أعضاء لكل بيت)، وكل XP ولقطة تكسبها تنحسب لبيتك.\n"
            "• يُمنع أي اتفاق أو تلاعب بين الأعضاء لتفضيل بيت معين أو التقاعس عمداً لصالح بيت آخر.\n"
            "• قائد البيت يسمّي بيته **مرة وحدة فقط** — التسمية نهائية ولا تتغير."
        ),
        inline=False
    )
    embed.add_field(
        name="⚠️ نظام التحذيرات (الإنذارات)",
        value=(
            f"• كل إنذار يُخصم منه تلقائياً `{WARNING_MONEY_PENALTY:,} {CURRENCY_NAME}` {CURRENCY_EMOJI} من رصيدك.\n"
            f"• عند الوصول لـ **{WARNINGS_AUTO_TIMEOUT_THRESHOLD}** إنذارات: تايم أوت تلقائي لمدة **{WARNINGS_AUTO_TIMEOUT_HOURS} ساعة**.\n"
            f"• عند الوصول لـ **{WARNINGS_ROLE_STRIP_THRESHOLD}** إنذارات: سحب تلقائي لرتبة العضوية.\n"
            f"• كل إنذار يُحذف تلقائياً بعد **{WARNING_EXPIRY_DAYS} يوم** من تاريخه إذا ما تكرر.\n"
            "• الإنذارات المتكررة أو التجاوزات الجسيمة قد تؤدي لطرد أو حظر دائم دون سابق إنذار."
        ),
        inline=False
    )
    embed.add_field(
        name="6️⃣ التذاكر والاقتراحات",
        value=(
            "• استخدم نظام التذاكر للتواصل الرسمي مع الإدارة، ويُمنع فتح تذاكر عبثية أو مكررة.\n"
            "• الاقتراحات تُطرح بروم الاقتراحات المخصص فقط ويُمنع إساءة استخدام التصويت عليها."
        ),
        inline=False
    )
    embed.add_field(
        name="7️⃣ صلاحية القرار",
        value=(
            "القائد والنائب والطاقم الإداري لهم صلاحية تفسير القوانين واتخاذ أي إجراء يرونه مناسباً "
            "لما يحفظ انضباط السيرفر، حتى لو لم يُذكر صراحة بالأعلى."
        ),
        inline=False
    )
    embed.set_image(url=BANNER_IMAGE_URL)
    apply_branding(embed, guild)
    return embed

class ServerRulesView(ButtonLimitedView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="القوانين الأساسية", emoji="📜", style=discord.ButtonStyle.primary,
                       custom_id="b4e_rules_basic")
    async def basic_rules(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=build_server_rules_embed(interaction.guild), ephemeral=True
        )

    @discord.ui.button(label="قوانين ستيم", emoji="🎮", style=discord.ButtonStyle.primary,
                       custom_id="b4e_rules_steam")
    async def steam_rules(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=build_steam_family_rules_embed(interaction.guild), ephemeral=True
        )


def build_rules_panel_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="📚 قوانين سيرفر B4E",
        description=(
            "مرحباً بك! اختر القسم من الأزرار بالأسفل لقراءة قوانينه.\n\n"
            "📜 **القوانين الأساسية**\n"
            "الاحترام، استخدام الرومات، أنظمة السيرفر، التذاكر والتحذيرات.\n\n"
            "🎮 **قوانين ستيم**\n"
            "شراء الألعاب، تقسيم المبالغ، الدفع، استخدام الفاميلي وحماية الحسابات.\n\n"
            "**الالتزام بالقوانين مسؤولية كل عضو، وعدم قراءتها لا يعفي من العقوبة.**"
        ),
        color=discord.Color.from_rgb(0, 168, 252)
    )
    embed.set_image(url=BANNER_IMAGE_URL)
    apply_branding(embed, guild)
    return embed


@bot.tree.command(name="server_rules", description="[إدارة] إرسال لوحة القوانين الأساسية وقوانين ستيم (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@is_owner_only()
async def server_rules_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=build_rules_panel_embed(interaction.guild), view=ServerRulesView()
    )

@bot.tree.command(name="steam_family_rules", description="عرض قوانين Steam Family")
async def steam_family_rules_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_steam_family_rules_embed(interaction.guild))

# 3.4.1 أوامر حرب البيوت الملكية
@bot.tree.command(name='clip_voters_allowed', description='عرض الأعضاء المعتمدين لتصويت اللقطات وشروط التصويت')
@app_commands.guild_only()
async def clip_voters_allowed(interaction: discord.Interaction):
    members = '\n'.join(f'• <@{user_id}> — `{user_id}`' for user_id in BEST_CLIP_VOTER_USER_IDS)
    embed = discord.Embed(title='الأعضاء المسموح لهم بتصويت اللقطات',
        description=members + '\n\nصوت نهائي واحد بالأسبوع، ممنوع التصويت للنفس، والفوز يحتاج تصويتين على الأقل.',
        color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="houses", description="عرض ترتيب البيوت الملكية الحالي في حرب الأسبوع")
async def houses_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    guild = interaction.guild
    active = await asyncio.to_thread(war_get_active_week_sync, guild.id)
    if not active:
        await interaction.followup.send("⚔️ ما فيه حرب بيوت نشطة حالياً.", ephemeral=True)
        return
    week_id = active[0]
    teams = await asyncio.to_thread(war_get_teams_sync, guild.id, week_id)
    ranked = sorted(teams, key=lambda t: (-t["score"], t["idx"]))
    now = datetime.datetime.now(RIYADH_TZ)
    end_ts = int((war_latest_boundary(now) + datetime.timedelta(days=7)).timestamp())
    lines = []
    for i, t in enumerate(ranked):
        name_note = "" if t["named"] else " *(بانتظار اسم القائد)*"
        leader_name = await war_display_name(guild, t["leader_id"])
        member_names = await war_names_list(guild, [m["id"] for m in t["members"]])
        lines.append(
            f"{WAR_MEDALS[min(i, len(WAR_MEDALS) - 1)]} **{war_house_label(t)}**{name_note} — `{t['score']:,}` نقطة\n"
            f"   👑 {leader_name} • 👥 {'، '.join(member_names)}"
        )
    embed = discord.Embed(
        title=f"⚔️ ترتيب حرب البيوت #{week_id}",
        description="\n\n".join(lines) + f"\n\n🎁 {war_prizes_text()}\n⏳ تنتهي <t:{end_ts}:R>",
        color=discord.Color.from_rgb(255, 215, 0)
    )
    apply_branding(embed, guild)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="royal_war_start", description="بدء حرب بيوت جديدة فوراً بتوزيع عشوائي جديد (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@is_owner_or_deputy()
async def royal_war_start_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok, msg = await start_royal_war(interaction.guild, started_by=interaction.user)
    await interaction.followup.send(("✅ " if ok else "❌ ") + msg, ephemeral=True)

@bot.tree.command(name="start_teams", description="بدء نظام الفرق (حرب البيوت) وتوزيع الرتب - لصاحب السيرفر فقط")
@app_commands.describe(cancel_current="لو فيه حرب شغالة: تلغيها بدون جوائز ولا نتائج وتبدأ جديدة")
@app_commands.default_permissions(administrator=True)
@is_owner_only()
async def start_teams_cmd(interaction: discord.Interaction, cancel_current: bool = False):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    async with war_lock:
        active = await asyncio.to_thread(war_get_active_week_sync, guild.id)
        if active and not cancel_current:
            await interaction.followup.send(
                f"❌ فيه حرب نشطة حالياً (#{active[0]}). أنهها بـ `/royal_war_end` أو شغّل الأمر مع خيار `cancel_current: True` لإلغائها وبدء جديدة.",
                ephemeral=True
            )
            return
        if active and cancel_current:
            await asyncio.to_thread(war_cancel_week_sync, guild.id, active[0], datetime.datetime.now(RIYADH_TZ).isoformat())
            cancel_log = discord.Embed(
                title=f"🚫 إلغاء حرب البيوت #{active[0]}",
                description="أُلغيت بدون نتائج ولا جوائز عشان تبدأ حرب جديدة.",
                color=discord.Color.orange()
            )
            await war_log(guild, cancel_log, target=interaction.user)
        ok, msg = await _start_royal_war_locked(guild, started_by=interaction.user)
    await interaction.followup.send(("✅ " if ok else "❌ ") + msg, ephemeral=True)

@bot.tree.command(name="royal_war_end", description="إنهاء حرب البيوت الحالية فوراً وإعلان النتائج وصرف الجوائز (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@is_owner_or_deputy()
async def royal_war_end_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok, msg = await finish_royal_war(interaction.guild, ended_by=interaction.user)
    await interaction.followup.send(("✅ " if ok else "❌ ") + msg, ephemeral=True)

# 3.5 أمر تشغيل فحص الألعاب المجانية فوراً (للأونر والنائب فقط) — مفيد للتشخيص بدل انتظار 20 دقيقة
@bot.tree.command(name="check_free_games_now", description="تشغيل فحص الألعاب المجانية فوراً بدل انتظار الفحص التلقائي (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@is_owner_or_deputy()
async def check_free_games_now(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not GIVEAWAY_CHANNEL_ID:
        await interaction.followup.send("❌ GIVEAWAY_CHANNEL_ID = 0 بالكود — لازم تحط آيدي الروم أول.", ephemeral=True)
        return
    channel = bot.get_channel(GIVEAWAY_CHANNEL_ID)
    if not channel:
        await interaction.followup.send(f"❌ ما لقيت روم بالآيدي `{GIVEAWAY_CHANNEL_ID}` — تأكد إن الآيدي صحيح وإن البوت يشوف الروم.", ephemeral=True)
        return

    await check_free_games_task()
    await interaction.followup.send(f"✅ تم تشغيل الفحص على روم {channel.mention}، راجع الكونسول للتفاصيل (نوع الاستجابة، عدد الألعاب المستلمة، وسبب أي تجاهل).", ephemeral=True)

# 4. أمر إضافة XP (للأونر والنائب مع إرسال رسالة التلفيل بروم التلفيل)
@bot.tree.command(name="add_xp", description="إضافة نقاط خبرة XP لعضو محدد (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="العضو المستهدف", xp="كمية الـ XP المراد إضافتها")
@is_owner_only()
async def add_xp(interaction: discord.Interaction, member: discord.Member, xp: int):
    await interaction.response.defer(ephemeral=True)
    if xp <= 0:
        await interaction.followup.send("❌ يرجى إدخال قيمة XP أكبر من صفر.", ephemeral=True)
        return

    new_level, leveled_up, _, levels_gained = await add_user_xp(interaction.guild.id, member.id, xp, is_voice=False)
    await update_member_level_role(member, new_level)

    reward = levels_gained * LEVEL_UP_REWARD if leveled_up else 0
    if reward > 0:
        await add_balance(interaction.guild.id, member.id, reward, reason='مكافأة ترقية المستوى')

    level_channel = bot.get_channel(LEVEL_CHANNEL_ID)
    if level_channel:
        embed_lvl = create_levelup_embed(member, new_level, interaction.guild, reward=reward)
        await level_channel.send(embed=embed_lvl)

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(title="✨ إضافة نقاط خبرة (XP)", color=discord.Color.green())
        embed.add_field(name="🛡️ الإداري", value=interaction.user.mention, inline=True)
        embed.add_field(name="👤 العضو المستهدف", value=member.mention, inline=True)
        embed.add_field(name="➕ النقاط المضافة", value=f"`+{xp} XP`", inline=True)
        apply_log_branding(embed, interaction.guild, target=interaction.user)
        await log_channel.send(embed=embed)

    channel_mention = log_channel.mention if log_channel else f"<#{LOG_CHANNEL_ID}>"
    reward_note = f" ونال `{reward:,} {CURRENCY_NAME}` كمكافأة ترقية." if reward > 0 else ""
    await interaction.followup.send(f"✅ تم إضافة **{xp} XP** للعضو {member.mention} (وصل لفل **{new_level}** وأخذ رتبته{reward_note}) وتم تسجيل العملية في {channel_mention}.", ephemeral=True)

# 5. أمر إزالة XP (للأونر والنائب)
@bot.tree.command(name="remove_xp", description="إزالة/خصم نقاط خبرة XP من عضو محدد (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="العضو المستهدف", xp="كمية الـ XP المراد إزالتها")
@is_owner_only()
async def remove_xp(interaction: discord.Interaction, member: discord.Member, xp: int):
    await interaction.response.defer(ephemeral=True)
    if xp <= 0:
        await interaction.followup.send("❌ يرجى إدخال قيمة XP أكبر من صفر.", ephemeral=True)
        return

    new_level, new_xp, level_changed = await remove_user_xp(interaction.guild.id, member.id, xp)
    await update_member_level_role(member, new_level)

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(title="🔻 خصم نقاط خبرة (XP)", color=discord.Color.red())
        embed.add_field(name="🛡️ الإداري", value=interaction.user.mention, inline=True)
        embed.add_field(name="👤 العضو المستهدف", value=member.mention, inline=True)
        embed.add_field(name="➖ النقاط المخصومة", value=f"`-{xp} XP`", inline=True)
        embed.add_field(name="🏆 المستوى الحالي", value=f"**Level {new_level}** `({new_xp} XP)`", inline=True)
        apply_log_branding(embed, interaction.guild, target=interaction.user)
        await log_channel.send(embed=embed)

    channel_mention = log_channel.mention if log_channel else f"<#{LOG_CHANNEL_ID}>"
    await interaction.followup.send(f"✅ تم خصم **{xp} XP** من العضو {member.mention} (مستواه الآن **Level {new_level}**) وتم تسجيل العملية في {channel_mention}.", ephemeral=True)

# 6. أمر تحديد لفل (للأونر والنائب)
@bot.tree.command(name="set_level", description="تحديد مستوى عضو مباشرة 0-50 (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="العضو المستهدف", level="المستوى الجديد (0-50)")
@is_owner_only()
async def set_level(interaction: discord.Interaction, member: discord.Member, level: int):
    await interaction.response.defer(ephemeral=True)
    if level < 0 or level > MAX_LEVEL:
        await interaction.followup.send(f"❌ يرجى إدخال مستوى بين 0 و {MAX_LEVEL}.", ephemeral=True)
        return

    with db_lock:
        cursor.execute("""
            INSERT INTO users (guild_id, user_id, xp, level, daily_voice_xp, last_voice_date)
            VALUES (?, ?, 0, ?, 0, '')
            ON CONFLICT(guild_id, user_id) 
            DO UPDATE SET level = ?, xp = 0
        """, (interaction.guild.id, member.id, level, level))
        conn.commit()

    await update_member_level_role(member, level)

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(title="⭐ تعديل مستوى عضو (Set Level)", color=discord.Color.blue())
        embed.add_field(name="🛡️ الإداري", value=interaction.user.mention, inline=True)
        embed.add_field(name="👤 العضو المستهدف", value=member.mention, inline=True)
        embed.add_field(name="🏆 المستوى الجديد", value=f"**Level {level}**", inline=True)
        apply_log_branding(embed, interaction.guild, target=interaction.user)
        await log_channel.send(embed=embed)

    channel_mention = log_channel.mention if log_channel else f"<#{LOG_CHANNEL_ID}>"
    await interaction.followup.send(f"✅ تم تعديل مستوى {member.mention} إلى **Level {level}** وأخذ رتبته وتم تسجيل العملية في {channel_mention}.", ephemeral=True)

# 7. أمر تصفير مستوى العضو (للأونر والنائب)
@bot.tree.command(name="reset_level", description="تصفير لفل وخبرة عضو بالكامل (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="العضو المستهدف")
@is_owner_only()
async def reset_level(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    with db_lock:
        cursor.execute("""
            UPDATE users 
            SET xp = 0, level = 0, daily_voice_xp = 0 
            WHERE guild_id = ? AND user_id = ?
        """, (interaction.guild.id, member.id))
        conn.commit()

    roles_to_remove = [interaction.guild.get_role(r_id) for r_id in LEVEL_ROLES.values() if r_id != 0 and interaction.guild.get_role(r_id) in member.roles]
    if roles_to_remove:
        try:
            await member.remove_roles(*roles_to_remove)
        except Exception:
            pass

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(title="🔄 تصفير مستوى عضو (Reset Level)", color=discord.Color.red())
        embed.add_field(name="🛡️ الإداري", value=interaction.user.mention, inline=True)
        embed.add_field(name="👤 العضو المستهدف", value=member.mention, inline=True)
        embed.add_field(name="📋 الحالة", value="تم تصفير نقاط الـ XP والمستوى وإزالة رتب التلفيل.", inline=False)
        apply_log_branding(embed, interaction.guild, target=interaction.user)
        await log_channel.send(embed=embed)

    channel_mention = log_channel.mention if log_channel else f"<#{LOG_CHANNEL_ID}>"
    await interaction.followup.send(f"✅ تم تصفير مستوى وخبرة {member.mention} بنجاح وتم تسجيل العملية في {channel_mention}.", ephemeral=True)

# 7.5. أمر تصحيح أي خلل قديم بين XP والمستوى (للأونر والنائب)
@bot.tree.command(name="fix_xp_desync", description="يفحص كل الأعضاء ويصحح أي حالة XP لا تطابق مستواهم الحالي (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@is_owner_only()
async def fix_xp_desync(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    changed = await normalize_all_users_xp(interaction.guild.id)

    if not changed:
        await interaction.followup.send("✅ فحصت كل الأعضاء، ما فيه أي خلل بين الـ XP والمستوى.", ephemeral=True)
        return

    updated_count = 0
    for user_id, old_level, new_level, new_xp in changed:
        member = interaction.guild.get_member(user_id)
        if member and new_level != old_level:
            try:
                await update_member_level_role(member, new_level)
            except Exception:
                pass
        updated_count += 1

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        lines = []
        for user_id, old_level, new_level, new_xp in changed[:20]:
            member = interaction.guild.get_member(user_id)
            who = member.mention if member else f"`{user_id}`"
            lines.append(f"{who}: LVL {old_level} ← أصبح LVL {new_level} (متبقي {new_xp:,} XP)")
        extra = f"\n...و{len(changed) - 20} عضو إضافي." if len(changed) > 20 else ""
        embed = discord.Embed(
            title="🛠️ تصحيح خلل XP/المستوى (Fix XP Desync)",
            description=(
                f"**الإداري:** {interaction.user.mention}\n"
                f"**عدد الأعضاء المصحّحين:** {len(changed)}\n\n" + "\n".join(lines) + extra
            ),
            color=discord.Color.orange()
        )
        apply_log_branding(embed, interaction.guild, target=interaction.user)
        try:
            await log_channel.send(embed=embed)
        except Exception:
            pass

    await interaction.followup.send(
        f"✅ تم فحص وتصحيح {updated_count} عضو كان الـ XP عندهم لا يطابق مستواهم الحالي، وتحدّثت رتب المستوى لهم تلقائيًا.",
        ephemeral=True
    )

# 8. أمر اختبار رسالة اللفل أب (للأونر والنائب)
@bot.tree.command(name="test_levelup", description="تجربة واختبار رسالة الترقية لمستوى معين 1-50 (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(level="رقم المستوى الذي تريد اختبار رسالته (1-50)", member="العضو المراد تجربة الترقية عليه (اختياري)")
@is_owner_only()
async def test_levelup(interaction: discord.Interaction, level: int, member: discord.Member = None):
    await interaction.response.defer(ephemeral=True)
    target = member or interaction.user
    level_channel = bot.get_channel(LEVEL_CHANNEL_ID)
    
    if not level_channel:
        await interaction.followup.send("❌ لم يتم العثور على روم التلفيل، تأكد من صحة LEVEL_CHANNEL_ID.", ephemeral=True)
        return

    embed = create_levelup_embed(target, level, interaction.guild)
    await level_channel.send(embed=embed)
    await update_member_level_role(target, level)

    await interaction.followup.send(f"✅ تم إرسال رسالة الترقية للمستوى **{level}** في {level_channel.mention} وتم تحديث الرتبة بنجاح.", ephemeral=True)

# أمر إعطاء عضو رتبة ومكافأة متصدر الأسبوع يدوياً (بتأثير فعلي كامل: رتبة + رصيد)
@bot.tree.command(name="test_weekly_top", description="إعطاء عضو رتبة متصدر الأسبوع ومكافأتها المالية يدوياً (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(scenario="نوع الرسالة المراد إرسالها", member="العضو المراد إعطاؤه الرتبة والمكافأة")
@app_commands.choices(scenario=[
    app_commands.Choice(name="متصدر جديد (أول مرة يتصدر)", value="new"),
    app_commands.Choice(name="تثبيت الصدارة (نفس الشخص يكمل)", value="keep"),
])
@is_owner_only()
async def test_weekly_top(interaction: discord.Interaction, scenario: app_commands.Choice[str], member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    target = member
    guild = interaction.guild

    if not WEEKLY_TOP_ROLE_ID:
        await interaction.followup.send("❌ WEEKLY_TOP_ROLE_ID = 0 بالكود — لازم تحط آيدي الرتبة أول.", ephemeral=True)
        return
    role = guild.get_role(WEEKLY_TOP_ROLE_ID)
    if not role:
        await interaction.followup.send(f"❌ ما لقيت رتبة بالآيدي `{WEEKLY_TOP_ROLE_ID}`.", ephemeral=True)
        return
    announce_channel = bot.get_channel(ROYAL_ANNOUNCE_CHANNEL_ID) if ROYAL_ANNOUNCE_CHANNEL_ID else None
    if not announce_channel:
        await interaction.followup.send("❌ ما لقيت روم بآيدي ROYAL_ANNOUNCE_CHANNEL_ID.", ephemeral=True)
        return

    # نشيل الرتبة من أي شخص ثاني حاطها حالياً (لو مو نفس الشخص)
    for m in list(role.members):
        if m.id != target.id:
            try:
                await m.remove_roles(role, reason=f"إعطاء الرتبة يدوياً لـ {target} عبر /test_weekly_top")
            except Exception as e:
                print(f"[Weekly Top Role] تعذّر إزالة الرتبة من {m}: {e}")

    if role not in target.roles:
        try:
            await target.add_roles(role, reason="إعطاء رتبة متصدر الأسبوع يدوياً")
        except Exception as e:
            await interaction.followup.send(f"❌ تعذّر إعطاء الرتبة: {e}", ephemeral=True)
            return

    if WEEKLY_TOP_ROLE_REWARD:
        await add_balance(guild.id, target.id, WEEKLY_TOP_ROLE_REWARD, reason='مكافأة متصدر الأسبوع')

    embed = build_weekly_top_embed(role, is_new=(scenario.value == "new"))
    embed.description = embed.description.replace("{mention}", target.mention)
    apply_branding(embed, guild, user=target)

    try:
        await announce_channel.send(content=target.mention, embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ حدث خطأ أثناء الإرسال: {e}", ephemeral=True)
        return

    await interaction.followup.send(f"✅ تم إعطاء {target.mention} رتبة {role.mention} و`{WEEKLY_TOP_ROLE_REWARD:,} {CURRENCY_NAME}` {CURRENCY_EMOJI}، وتم نشر الإعلان بروم {announce_channel.mention}.", ephemeral=True)

# ----------------- أوامر نظام الإنذارات (Warnings) -----------------
@bot.tree.command(name="warn", description="إعطاء إنذار لعضو مع تسجيله بقاعدة البيانات (للأونر والنائب ورتبة الإنذار الإضافية)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(member="العضو المراد إنذاره", reason="سبب الإنذار")
@is_owner_or_deputy_or_warn_role()
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    await interaction.response.defer(ephemeral=True)

    if member.bot:
        await interaction.followup.send("❌ ما تقدر تنذر بوت.", ephemeral=True)
        return

    warn_id = add_warning(interaction.guild.id, member.id, interaction.user.id, reason)
    total = count_warnings(interaction.guild.id, member.id)

    # ----- خصم مالي تلقائي من رصيد العضو مع كل إنذار -----
    new_balance = None
    if WARNING_MONEY_PENALTY > 0:
        new_balance = await add_balance(interaction.guild.id, member.id, -WARNING_MONEY_PENALTY, reason='خصم إنذار')

    # ----- تايم أوت تلقائي عند الوصول لعدد إنذارات معيّن -----
    auto_timeout_applied = False
    if total == WARNINGS_AUTO_TIMEOUT_THRESHOLD:
        try:
            await member.timeout(
                discord.utils.utcnow() + datetime.timedelta(hours=WARNINGS_AUTO_TIMEOUT_HOURS),
                reason=f"تايم أوت تلقائي: وصل لـ {WARNINGS_AUTO_TIMEOUT_THRESHOLD} إنذارات"
            )
            auto_timeout_applied = True
        except discord.Forbidden:
            print(f"[Auto Timeout] ما قدرت أعطي {member} تايم أوت تلقائي (صلاحيات/تسلسل رتب).")
        except Exception as e:
            print(f"[Auto Timeout] خطأ أثناء إعطاء تايم أوت تلقائي لـ {member}: {e}")

    # ----- سحب رول العضوية تلقائياً عند الوصول لعدد إنذارات معيّن -----
    role_stripped = False
    if total == WARNINGS_ROLE_STRIP_THRESHOLD:
        strip_role = interaction.guild.get_role(WARNING_STRIP_ROLE_ID)
        if strip_role and strip_role in member.roles:
            try:
                await member.remove_roles(strip_role, reason=f"سحب تلقائي: وصل لـ {WARNINGS_ROLE_STRIP_THRESHOLD} إنذارات")
                role_stripped = True
            except discord.Forbidden:
                print(f"[Auto Role Strip] ما قدرت أسحب الرول من {member} (صلاحيات/تسلسل رتب).")
            except Exception as e:
                print(f"[Auto Role Strip] خطأ أثناء سحب الرول من {member}: {e}")

    try:
        dm_desc = f"استلمت إنذاراً بسيرفر **{interaction.guild.name}**.\n\n📝 **السبب:** {reason}\n🔢 **رقم الإنذار:** #{warn_id}\n📊 **إجمالي إنذاراتك:** {total}"
        if WARNING_MONEY_PENALTY > 0 and new_balance is not None:
            dm_desc += f"\n💸 **خصم تلقائي:** `-{WARNING_MONEY_PENALTY:,} {CURRENCY_NAME}` (رصيدك الآن `{new_balance:,} {CURRENCY_NAME}`)"
        if auto_timeout_applied:
            dm_desc += f"\n\n⏳ بسبب وصولك لـ **{WARNINGS_AUTO_TIMEOUT_THRESHOLD}** إنذارات، تم إعطاؤك تايم أوت تلقائي لمدة **{WARNINGS_AUTO_TIMEOUT_HOURS}** ساعة."
        if role_stripped:
            dm_desc += f"\n\n🚫 بسبب وصولك لـ **{WARNINGS_ROLE_STRIP_THRESHOLD}** إنذارات، تم سحب رول {strip_role.name} منك تلقائياً."
        dm_embed = discord.Embed(
            title="⚠️ تم إنذارك",
            description=dm_desc,
            color=discord.Color.orange()
        )
        await member.send(embed=dm_embed)
    except Exception:
        pass

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(title="⚠️ إنذار عضو", color=discord.Color.orange())
        embed.add_field(name="🛡️ الإداري", value=interaction.user.mention, inline=True)
        embed.add_field(name="👤 العضو", value=member.mention, inline=True)
        embed.add_field(name="🔢 رقم الإنذار", value=f"#{warn_id}", inline=True)
        embed.add_field(name="📊 إجمالي إنذاراته", value=str(total), inline=True)
        embed.add_field(name="📝 السبب", value=reason, inline=False)
        if WARNING_MONEY_PENALTY > 0 and new_balance is not None:
            embed.add_field(name="💸 خصم مالي", value=f"`-{WARNING_MONEY_PENALTY:,} {CURRENCY_NAME}` (الرصيد الآن `{new_balance:,} {CURRENCY_NAME}`)", inline=False)
        if auto_timeout_applied:
            embed.add_field(name="⏳ إجراء تلقائي", value=f"تايم أوت **{WARNINGS_AUTO_TIMEOUT_HOURS} ساعة** (وصل لـ {WARNINGS_AUTO_TIMEOUT_THRESHOLD} إنذارات)", inline=False)
        if role_stripped:
            embed.add_field(name="🚫 إجراء تلقائي", value=f"سُحب رول {strip_role.name} (وصل لـ {WARNINGS_ROLE_STRIP_THRESHOLD} إنذارات)", inline=False)
        apply_log_branding(embed, interaction.guild, target=interaction.user)
        # منشن فعلي (يرسل إشعار) للعضو المُنذَر بروم السجلات المهمة، بالإضافة للإيمبيد
        await log_channel.send(content=f"|| {member.mention} ||", embed=embed)

    # ----- إرسال إشعار الإنذار لروم مخصص (بالإضافة لروم السجلات) -----
    warn_announce_channel = bot.get_channel(WARNING_ANNOUNCE_CHANNEL_ID)
    if warn_announce_channel:
        announce_embed = discord.Embed(title="⚠️ إنذار عضو", color=discord.Color.orange())
        announce_embed.add_field(name="🛡️ الإداري", value=interaction.user.mention, inline=True)
        announce_embed.add_field(name="👤 العضو", value=member.mention, inline=True)
        announce_embed.add_field(name="🔢 رقم الإنذار", value=f"#{warn_id}", inline=True)
        announce_embed.add_field(name="📊 إجمالي إنذاراته", value=str(total), inline=True)
        announce_embed.add_field(name="📝 السبب", value=reason, inline=False)
        if auto_timeout_applied:
            announce_embed.add_field(name="⏳ إجراء تلقائي", value=f"تايم أوت **{WARNINGS_AUTO_TIMEOUT_HOURS} ساعة** (وصل لـ {WARNINGS_AUTO_TIMEOUT_THRESHOLD} إنذارات)", inline=False)
        if role_stripped:
            announce_embed.add_field(name="🚫 إجراء تلقائي", value=f"سُحب رول {strip_role.name} (وصل لـ {WARNINGS_ROLE_STRIP_THRESHOLD} إنذارات)", inline=False)
        apply_log_branding(announce_embed, interaction.guild, target=interaction.user)
        try:
            await warn_announce_channel.send(embed=announce_embed)
        except Exception as e:
            print(f"[Warn Announce] خطأ أثناء إرسال إشعار الإنذار: {e}")

    channel_mention = log_channel.mention if log_channel else f"<#{LOG_CHANNEL_ID}>"
    extra_note = f" وأُعطي تايم أوت تلقائي {WARNINGS_AUTO_TIMEOUT_HOURS} ساعة" if auto_timeout_applied else ""
    if role_stripped:
        extra_note += f" وتم سحب رول {strip_role.name}"
    if WARNING_MONEY_PENALTY > 0 and new_balance is not None:
        extra_note += f" وتم خصم `{WARNING_MONEY_PENALTY:,} {CURRENCY_NAME}` من رصيده (الآن `{new_balance:,} {CURRENCY_NAME}`)"
    await interaction.followup.send(f"✅ تم إنذار {member.mention} (الإنذار رقم #{warn_id}، إجمالي **{total}** إنذار{extra_note}) وتم تسجيله في {channel_mention}.", ephemeral=True)

@bot.tree.command(name="warnings", description="عرض سجل إنذارات عضو (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(member="العضو المراد عرض إنذاراته (اتركه فاضي لعرض إنذاراتك أنت)")
@is_owner_or_deputy()
async def warnings_cmd(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user

    await interaction.response.defer(ephemeral=True)
    rows = get_warnings(interaction.guild.id, target.id)

    embed = discord.Embed(
        title=f"⚠️ سجل إنذارات {target.display_name}",
        color=discord.Color.orange() if rows else discord.Color.green()
    )
    apply_branding(embed, interaction.guild, user=target)

    if not rows:
        embed.description = "✅ لا يوجد أي إنذار مسجل لهذا العضو."
    else:
        for warn_id, moderator_id, reason, timestamp in rows[-25:]:
            mod_mention = f"<@{moderator_id}>"
            try:
                ts_display = datetime.datetime.fromisoformat(timestamp).strftime("%Y-%m-%d %H:%M")
            except Exception:
                ts_display = timestamp
            embed.add_field(
                name=f"إنذار #{warn_id} • {ts_display}",
                value=f"👮 بواسطة: {mod_mention}\n📝 السبب: {reason}",
                inline=False
            )
        embed.set_footer(text=f"إجمالي الإنذارات: {len(rows)}")

    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="remove_warning", description="حذف إنذار معيّن برقمه من سجل عضو (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(warn_id="رقم الإنذار المراد حذفه (يظهر بأمر /warnings)")
@is_owner_or_deputy()
async def remove_warning(interaction: discord.Interaction, warn_id: int):
    await interaction.response.defer(ephemeral=True)
    removed_user_id = remove_warning_by_id(interaction.guild.id, warn_id)

    if removed_user_id is None:
        await interaction.followup.send(f"❌ ما فيه إنذار برقم #{warn_id} بهذا السيرفر.", ephemeral=True)
        return

    removed_member = interaction.guild.get_member(removed_user_id)

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(title="🗑️ حذف إنذار", color=discord.Color.blue())
        embed.add_field(name="🛡️ الإداري", value=interaction.user.mention, inline=True)
        embed.add_field(name="👤 صاحب الإنذار", value=removed_member.mention if removed_member else f"<@{removed_user_id}>", inline=True)
        embed.add_field(name="🔢 رقم الإنذار المحذوف", value=f"#{warn_id}", inline=True)
        apply_log_branding(embed, interaction.guild, target=interaction.user)
        await log_channel.send(embed=embed)

    await interaction.followup.send(f"✅ تم حذف الإنذار رقم #{warn_id}.", ephemeral=True)

@bot.tree.command(name="clear_warnings", description="حذف كل إنذارات عضو معيّن دفعة وحدة (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(member="العضو المراد تصفير إنذاراته")
@is_owner_or_deputy()
async def clear_warnings_cmd(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    removed_count = clear_warnings(interaction.guild.id, member.id)

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(title="🧹 تصفير إنذارات عضو", color=discord.Color.blue())
        embed.add_field(name="🛡️ الإداري", value=interaction.user.mention, inline=True)
        embed.add_field(name="👤 العضو", value=member.mention, inline=True)
        embed.add_field(name="🗑️ عدد الإنذارات المحذوفة", value=str(removed_count), inline=True)
        apply_log_branding(embed, interaction.guild, target=interaction.user)
        await log_channel.send(embed=embed)

    await interaction.followup.send(f"✅ تم حذف **{removed_count}** إنذار من سجل {member.mention}.", ephemeral=True)

MOD_ACTION_LABELS = {
    "kick": ("👢", "طرد"),
    "ban": ("🔨", "حظر"),
    "unban": ("🔓", "رفع حظر"),
    "timeout": ("⏳", "تايم أوت"),
    "timeout_remove": ("✅", "إنهاء تايم أوت"),
}

@bot.tree.command(name="modlogs", description="عرض السجل الإداري الكامل لعضو: إنذارات + طرد + حظر + تايم أوت (تشوف سجلك، والإدارة تشوف أي عضو)")
@app_commands.describe(
    member="العضو المراد عرض سجله (لو لسا موجود بالسيرفر)",
    user_id="آيدي العضو لو غادر السيرفر أو محظور حالياً (استخدمه بدل member)"
)
async def modlogs_cmd(interaction: discord.Interaction, member: discord.Member = None, user_id: str = None):
    target_id = None
    target_display = None
    target_avatar_url = None

    if member:
        target_id = member.id
        target_display = member.display_name
        target_avatar_url = member.display_avatar.url
    elif user_id:
        try:
            target_id = int(user_id.strip())
        except ValueError:
            await interaction.response.send_message("❌ آيدي غير صالح، تأكد إنه أرقام بس.", ephemeral=True)
            return
    else:
        target_id = interaction.user.id
        target_display = interaction.user.display_name
        target_avatar_url = interaction.user.display_avatar.url

    if target_id != interaction.user.id and not is_staff_member(interaction.user):
        await interaction.response.send_message("❌ ما تقدر تشوف السجل الإداري لعضو غيرك.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    if target_display is None:
        try:
            fetched_user = await bot.fetch_user(target_id)
            target_display = fetched_user.display_name
            target_avatar_url = fetched_user.display_avatar.url
        except Exception:
            target_display = f"عضو غادر ({target_id})"
            target_avatar_url = None

    warn_rows = get_warnings(interaction.guild.id, target_id)
    action_rows = get_mod_actions(interaction.guild.id, target_id)

    timeline = []
    for warn_id, moderator_id, reason, timestamp in warn_rows:
        timeline.append((timestamp, "⚠️", f"إنذار #{warn_id}", moderator_id, reason))
    for action_id, moderator_id, action_type, reason, timestamp in action_rows:
        emoji, label = MOD_ACTION_LABELS.get(action_type, ("📌", action_type))
        timeline.append((timestamp, emoji, label, moderator_id, reason))
    timeline.sort(key=lambda row: row[0])

    embed = discord.Embed(
        title=f"📁 السجل الإداري الكامل • {target_display}",
        color=discord.Color.blurple() if timeline else discord.Color.green()
    )
    if target_avatar_url:
        embed.set_thumbnail(url=target_avatar_url)

    status_lines = []
    live_member = interaction.guild.get_member(target_id)
    if live_member and live_member.timed_out_until and live_member.timed_out_until > discord.utils.utcnow():
        status_lines.append(f"⏳ **بتايم أوت حالياً** حتى <t:{int(live_member.timed_out_until.timestamp())}:R>")
    try:
        ban_entry = await interaction.guild.fetch_ban(discord.Object(id=target_id))
        status_lines.append(f"🔨 **محظور حالياً بالسيرفر** — السبب: {ban_entry.reason or 'غير مذكور'}")
    except (discord.NotFound, discord.HTTPException):
        pass
    if status_lines:
        embed.add_field(name="📍 الحالة الحالية", value="\n".join(status_lines), inline=False)

    if not timeline:
        embed.description = "✅ لا يوجد أي إجراء إداري مسجل بحق هذا العضو."
    else:
        for timestamp, emoji, label, moderator_id, reason in list(reversed(timeline))[:15]:
            try:
                ts_display = datetime.datetime.fromisoformat(timestamp).strftime("%Y-%m-%d %H:%M")
            except Exception:
                ts_display = timestamp
            mod_mention = f"<@{moderator_id}>" if moderator_id else "غير معروف / تلقائي"
            embed.add_field(
                name=f"{emoji} {label} • {ts_display}",
                value=f"👮 بواسطة: {mod_mention}\n📝 السبب: {reason or 'لم يُذكر سبب'}",
                inline=False
            )
        embed.set_footer(text=f"إجمالي الإجراءات المسجلة: {len(timeline)} (آخر 15 معروضة، الأحدث أولاً)")

    await interaction.followup.send(embed=embed, ephemeral=True)

# ----------------- أمر نظام تتبع الدعوات (Invite Tracker) -----------------
@bot.tree.command(name="invites", description="عرض عدد الدعوات الناجحة لعضو (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(member="العضو المراد عرض دعواته (اتركه فاضي لعرض دعواتك أنت)")
@is_owner_or_deputy()
async def invites_cmd(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    await interaction.response.defer(ephemeral=True)

    total = get_invite_count(interaction.guild.id, target.id)
    embed = discord.Embed(
        title="🔗 إحصائية الدعوات",
        description=f"{target.mention} جاب **{total}** {'عضو' if total != 1 else 'عضو'} للسيرفر عن طريق دعواته.",
        color=discord.Color.blurple()
    )
    apply_branding(embed, interaction.guild, user=target)
    await interaction.followup.send(embed=embed, ephemeral=True)

# ----------------- أمر النسخ الاحتياطي اليدوي لقاعدة البيانات -----------------
@bot.tree.command(name="backup_now", description="إرسال نسخة احتياطية فورية من قاعدة بيانات البوت (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@is_owner_or_deputy()
async def backup_now(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        path,uploaded=await save_and_send_safety_backup('نسخة يدوية قبل التعديل أو الإغلاق',interaction.user)
    except Exception as exc:
        print(f'[Safety Backup] {exc}')
        await interaction.followup.send('تعذر إنشاء نسخة سليمة؛ راجع سجل التشغيل ومساحة التخزين قبل التحديث.',ephemeral=True)
        return
    destination=f"وأُرسلت إلى <#{LOG_CHANNEL_ID}>" if uploaded else 'محليًا فقط؛ تعذر إرسالها لروم السجلات'
    await interaction.followup.send(f"✅ حُفظ السكربت والبيانات والأوامر {destination}.\nالنسخة: `backups/{os.path.basename(path)}`",ephemeral=True)

@bot.tree.command(name='prepare_update',description='حفظ السكربت والبيانات والأوامر قبل التحديث أو إطفاء البوت')
@app_commands.default_permissions(manage_guild=True)
@is_owner_or_deputy()
async def prepare_update_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        path,uploaded=await save_and_send_safety_backup('نسخة تحضير للتحديث أو الإغلاق',interaction.user)
    except Exception as exc:
        print(f'[Safety Backup] فشل التحضير: {exc}')
        await interaction.followup.send('❌ فشل حفظ نسخة سليمة. راجع سجل التشغيل قبل إطفاء البوت أو تعديل ملفاته.',ephemeral=True)
        return
    note=f"أُرسلت النسخة إلى <#{LOG_CHANNEL_ID}>." if uploaded else 'النسخة محفوظة محليًا، لكن تعذر إرسالها لروم السجلات؛ حمّلها من ملفات الاستضافة.'
    await interaction.followup.send(f"✅ تم حفظ السكربت وقاعدة البيانات وقائمة الأوامر. {note}\nالنسخة: `backups/{os.path.basename(path)}`\nتقدر توقف البوت وتعدّل ملفاته. خلك محافظ على `levels.db` و`.env` ومجلد `backups`.",ephemeral=True)

# ----------------- أوامر نظام العملة (البنك + الرواتب) -----------------
@bot.tree.command(name="balance", description="عرض رصيدك من البنك (تقدر تشوف رصيدك، والأونر/النائب يشوفون أي عضو)")
@app_commands.describe(member="العضو المراد عرض رصيده (اتركه فاضي لعرض رصيدك أنت)")
async def balance_cmd(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user

    if target.id != interaction.user.id and not is_staff_member(interaction.user):
        await interaction.response.send_message("❌ ما تقدر تشوف رصيد عضو غيرك.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    bal = await get_balance(interaction.guild.id, target.id)
    embed = discord.Embed(
        title=f"{CURRENCY_EMOJI} كشف البنك",
        description=f"رصيد {target.mention}: **{bal:,} {CURRENCY_NAME}**",
        color=discord.Color.from_rgb(241, 196, 15)
    )
    apply_branding(embed, interaction.guild, user=target)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="add_balance", description="إضافة رصيد لعضو محدد (لصاحب السيرفر/القائد فقط)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(member="العضو المستهدف", amount=f"المبلغ المراد إضافته بـ{CURRENCY_NAME}")
@is_owner_only()
async def add_balance_cmd(interaction: discord.Interaction, member: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    if amount <= 0:
        await interaction.followup.send("❌ يرجى إدخال مبلغ أكبر من صفر.", ephemeral=True)
        return

    new_balance = await add_balance(interaction.guild.id, member.id, amount, reason='إضافة إدارية')

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(title=f"{CURRENCY_EMOJI} إضافة رصيد", color=discord.Color.green())
        embed.add_field(name="🛡️ الإداري", value=interaction.user.mention, inline=True)
        embed.add_field(name="👤 العضو المستهدف", value=member.mention, inline=True)
        embed.add_field(name="➕ المبلغ المضاف", value=f"`+{amount:,} {CURRENCY_NAME}`", inline=True)
        embed.add_field(name="💼 الرصيد الحالي", value=f"`{new_balance:,} {CURRENCY_NAME}`", inline=True)
        apply_log_branding(embed, interaction.guild, target=member)
        await log_channel.send(embed=embed)

    channel_mention = log_channel.mention if log_channel else f"<#{LOG_CHANNEL_ID}>"
    await interaction.followup.send(
        f"✅ تم إضافة `{amount:,} {CURRENCY_NAME}` للعضو {member.mention} (رصيده الآن `{new_balance:,} {CURRENCY_NAME}`) وتم تسجيل العملية في {channel_mention}.",
        ephemeral=True
    )

@bot.tree.command(name="remove_balance", description="خصم رصيد من عضو محدد (لصاحب السيرفر/القائد فقط)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(member="العضو المستهدف", amount=f"المبلغ المراد خصمه بـ{CURRENCY_NAME}")
@is_owner_only()
async def remove_balance_cmd(interaction: discord.Interaction, member: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    if amount <= 0:
        await interaction.followup.send("❌ يرجى إدخال مبلغ أكبر من صفر.", ephemeral=True)
        return

    new_balance = await add_balance(interaction.guild.id, member.id, -amount, reason='خصم إداري')

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(title=f"{CURRENCY_EMOJI} خصم رصيد", color=discord.Color.red())
        embed.add_field(name="🛡️ الإداري", value=interaction.user.mention, inline=True)
        embed.add_field(name="👤 العضو المستهدف", value=member.mention, inline=True)
        embed.add_field(name="➖ المبلغ المخصوم", value=f"`-{amount:,} {CURRENCY_NAME}`", inline=True)
        embed.add_field(name="💼 الرصيد الحالي", value=f"`{new_balance:,} {CURRENCY_NAME}`", inline=True)
        apply_log_branding(embed, interaction.guild, target=member)
        await log_channel.send(embed=embed)

    channel_mention = log_channel.mention if log_channel else f"<#{LOG_CHANNEL_ID}>"
    await interaction.followup.send(
        f"✅ تم خصم `{amount:,} {CURRENCY_NAME}` من العضو {member.mention} (رصيده الآن `{new_balance:,} {CURRENCY_NAME}`، ما ينزل تحت الصفر) وتم تسجيل العملية في {channel_mention}.",
        ephemeral=True
    )

@bot.tree.command(name="all_balances", description="عرض رصيد كل أعضاء السيرفر كملف نصي مرتّب من الأعلى للأقل (لصاحب السيرفر فقط)")
@app_commands.default_permissions(manage_guild=True)
@is_owner_only()
async def all_balances_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # نجيب كل أرصدة السيرفر المسجّلة بقاعدة البيانات دفعة وحدة
    with db_lock:
        cursor.execute("SELECT user_id, balance FROM wallets WHERE guild_id = ?", (guild.id,))
        wallet_rows = cursor.fetchall()
    balances_by_id = {uid: bal for uid, bal in wallet_rows}

    # نضيف بقية أعضاء السيرفر اللي ما عندهم صف بالبنك أصلاً (رصيدهم 0)، ونستثني البوتات
    combined = []
    seen_ids = set()
    for member in guild.members:
        if member.bot:
            continue
        combined.append((member.id, member.display_name, balances_by_id.get(member.id, 0)))
        seen_ids.add(member.id)
    # أعضاء عندهم رصيد بقاعدة البيانات لكن غادروا السيرفر (ما زالوا نلحقهم بالقائمة تحت اسم غير معروف)
    for uid, bal in wallet_rows:
        if uid not in seen_ids:
            combined.append((uid, f"عضو غادر (ID: {uid})", bal))

    combined.sort(key=lambda row: row[2], reverse=True)

    total_balance = sum(row[2] for row in combined)
    lines = [
        f"كشف أرصدة جميع أعضاء سيرفر {guild.name}",
        f"تاريخ التصدير: {datetime.datetime.now(RIYADH_TZ).strftime('%Y-%m-%d %H:%M')} (توقيت الرياض)",
        f"عدد الأعضاء: {len(combined)}  |  إجمالي الأرصدة: {total_balance:,} {CURRENCY_NAME}",
        "-" * 60,
    ]
    for rank, (uid, name, bal) in enumerate(combined, start=1):
        lines.append(f"{rank}. {name} (ID: {uid}) — {bal:,} {CURRENCY_NAME}")

    file_content = "\n".join(lines)
    file_buffer = io.BytesIO(file_content.encode("utf-8"))
    discord_file = discord.File(fp=file_buffer, filename="all_balances.txt")

    await interaction.followup.send(
        content=f"💰 كشف أرصدة كل أعضاء **{guild.name}** ({len(combined)} عضو):",
        file=discord_file,
        ephemeral=True
    )

cursor.execute("""CREATE TABLE IF NOT EXISTS roulette_spin_history (
    guild_id INTEGER, user_id INTEGER, spin_date TEXT, created_ts REAL,
    result_label TEXT, reward INTEGER, balance_after INTEGER, retries INTEGER, trail TEXT,
    PRIMARY KEY (guild_id, user_id, spin_date)
)""")
conn.commit()


def settle_roulette_spin_sync(guild_id, user_id, now_ts, today):
    import json
    with db_lock:
        try:
            previous = conn.execute('SELECT last_date FROM roulette_cooldowns WHERE guild_id=? AND user_id=?',
                                    (guild_id, user_id)).fetchone()
            if previous and previous[0] == today:
                return None
            trail = []
            retries = 0
            while True:
                choices = ROULETTE_SEGMENTS if retries < ROULETTE_MAX_RETRIES else [x for x in ROULETTE_SEGMENTS if x[2] != 'retry']
                label, emoji, kind, value, weight, color = random.choices(choices, weights=[x[4] for x in choices], k=1)[0]
                trail.append(label)
                if kind != 'retry':
                    break
                retries += 1
            reward = value if kind == 'win' else 0
            row = conn.execute('SELECT balance FROM wallets WHERE guild_id=? AND user_id=?', (guild_id, user_id)).fetchone()
            balance = (row[0] if row else 0) + reward
            if reward:
                conn.execute('INSERT INTO wallets(guild_id,user_id,balance) VALUES(?,?,?) '
                             'ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=excluded.balance',
                             (guild_id, user_id, balance))
                record_bank_transaction_locked(guild_id, user_id, reward, balance, 'ربح الروليت')
            conn.execute('INSERT INTO roulette_cooldowns VALUES(?,?,?) '
                         'ON CONFLICT(guild_id,user_id) DO UPDATE SET last_date=excluded.last_date',
                         (guild_id, user_id, today))
            conn.execute('INSERT INTO roulette_spin_history VALUES(?,?,?,?,?,?,?,?,?)',
                         (guild_id, user_id, today, now_ts, label, reward, balance, retries, json.dumps(trail, ensure_ascii=False)))
            conn.commit()
            return label, reward, balance, retries, trail
        except Exception:
            conn.rollback()
            raise


def get_roulette_history_sync(guild_id, user_id):
    with db_lock:
        return conn.execute('SELECT created_ts,result_label,reward,balance_after,retries '
                            'FROM roulette_spin_history WHERE guild_id=? AND user_id=? '
                            'ORDER BY created_ts DESC LIMIT 10', (guild_id, user_id)).fetchall()


def build_roulette_panel_embed(guild):
    embed = discord.Embed(title='🎡 روليت B4E | حظك اليومي',
        description='جرّب حظك بدورة واحدة يوميًا، وتابع نتيجتك وسجل دوراتك من الأزرار بالأسفل.',
        color=discord.Color.from_rgb(36, 125, 220))
    embed.add_field(name='💰 الجوائز', value=f'من **5** إلى **50 {CURRENCY_NAME}** {CURRENCY_EMOJI}', inline=True)
    embed.add_field(name='🕛 تجدد المحاولة', value='منتصف الليل بتوقيت الرياض', inline=True)
    embed.add_field(name='🔁 إعادة مجانية', value='تظهر لك نتيجة «إعادة»، ثم يدور البوت تلقائيًا ويعرض النتيجة الجديدة.', inline=False)
    embed.add_field(name='📜 سجل دوراتك', value='آخر 10 دورات بالتاريخ والمكافأة وعدد الإعادات؛ تظهر لك فقط.', inline=False)
    embed.add_field(name='💀 هارد لك', value='بدون مكافأة لهذه الدورة؛ تقدر تجرب مرة ثانية بكرا.', inline=False)
    apply_branding(embed, guild)
    return embed


async def run_roulette_spin(interaction: discord.Interaction, ephemeral: bool = False):
    guild, user = interaction.guild, interaction.user
    if not guild:
        await interaction.followup.send('الروليت متاح داخل السيرفر فقط.', ephemeral=True)
        return
    if user.id not in ROULETTE_ALLOWED_USER_IDS:
        await interaction.followup.send('❌ الروليت متاح للأعضاء المعتمدين فقط.', ephemeral=True)
        return
    now = datetime.datetime.now(RIYADH_TZ)
    next_reset = int((now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    outcome = await asyncio.to_thread(settle_roulette_spin_sync, guild.id, user.id, now.timestamp(), now.date().isoformat())
    if outcome is None:
        await interaction.followup.send(f'خلصت دورتك اليوم. محاولتك الجاية <t:{next_reset}:R>. تقدر تشوف نتيجتك من «آخر 10 دورات».', ephemeral=True)
        return
    label, reward, balance, retries, trail = outcome
    message = None
    for index, step in enumerate(trail):
        is_retry = index < len(trail) - 1
        animation = None
        try:
            animation = discord.File(await render_roulette_spin_gif(step), filename=f'roulette_{index}.gif')
        except Exception as exc:
            print(f'[Roulette Render] {exc}')
        if is_retry:
            embed = discord.Embed(title='🔁 طلعت لك إعادة!',
                description=f'إعادة مجانية رقم **{index + 1}**. البوت بيدور لك مرة ثانية تلقائيًا…',
                color=discord.Color.from_rgb(36, 125, 220))
        else:
            embed = discord.Embed(title='🎉 مبروك! ربحت' if reward else '💀 هارد لك، حظ أوفر بكرا',
                description='هذه نتيجة دورتك اليومية.',
                color=discord.Color.from_rgb(42, 180, 120) if reward else discord.Color.from_rgb(220, 85, 105))
            embed.add_field(name='🎯 النتيجة', value=f'**+{reward} {CURRENCY_NAME}**' if reward else '**هارد لك — 0 ريال**', inline=True)
            embed.add_field(name='💰 رصيدك بعد الدورة', value=f'{balance:,} {CURRENCY_NAME} {CURRENCY_EMOJI}', inline=True)
            if retries:
                embed.add_field(name='🔁 الإعادات المجانية', value=f'{retries} قبل النتيجة النهائية', inline=True)
            embed.add_field(name='🕛 محاولتك الجاية', value=f'<t:{next_reset}:R>', inline=False)
        apply_branding(embed, guild, user=user)
        embed.set_thumbnail(url=user.display_avatar.url)
        if animation:
            embed.set_image(url=f'attachment://{animation.filename}')
        try:
            if message is None:
                options = {'embed': embed, 'ephemeral': ephemeral, 'wait': True}
                if animation: options['file'] = animation
                message = await interaction.followup.send(**options)
            else:
                await message.edit(embed=embed, attachments=[animation] if animation else [])
        finally:
            if animation: animation.close()
        if is_retry:
            await asyncio.sleep(6)
    if reward >= ROULETTE_BIG_WIN_THRESHOLD:
        channel = bot.get_channel(ROULETTE_BIG_WIN_CHANNEL_ID)
        if channel:
            announcement = discord.Embed(title='🎊 فوز كبير بالروليت!',
                description=f'{user.mention} ربح **{reward} {CURRENCY_NAME}** {CURRENCY_EMOJI}، مبروك!', color=discord.Color.gold())
            apply_branding(announcement, guild, user=user)
            try: await channel.send(embed=announcement)
            except discord.HTTPException as exc: print(f'[Roulette Announcement] {exc}')
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        log = discord.Embed(title='🎡 نتيجة الروليت اليومي', color=discord.Color.blue())
        log.add_field(name='👤 العضو', value=user.mention)
        log.add_field(name='🎯 النتيجة', value=f'{label} | +{reward} {CURRENCY_NAME}')
        log.add_field(name='🔁 الإعادات', value=str(retries))
        log.add_field(name='💰 الرصيد', value=f'{balance:,} {CURRENCY_NAME}')
        apply_log_branding(log, guild, target=user)
        try: await log_channel.send(embed=log)
        except discord.HTTPException as exc: print(f'[Roulette Log] {exc}')

@bot.tree.command(name="roulette", description=f"دور الروليت وجرب حظك تربح من 5 إلى 50 {CURRENCY_NAME} (للقائد فقط — بقية الأعضاء يستخدمون زر لوحة الروليت)")
@app_commands.default_permissions(administrator=True)
@is_owner_only()
async def roulette_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    await run_roulette_spin(interaction)

# ----------------- لوحة الروليت الثابتة (زر يدور بدون كتابة أمر) -----------------
class RoulettePanelView(ButtonLimitedView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="دور الروليت",
        emoji="🎰",
        style=discord.ButtonStyle.primary,
        custom_id="roulette_panel_spin_btn"
    )
    async def spin_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ديفر بـ ephemeral عشان نتيجة الدورة ما يشوفها إلا اللي ضغط الزر بس
        await interaction.response.defer(ephemeral=True)
        await run_roulette_spin(interaction, ephemeral=True)

    @discord.ui.button(label="آخر 10 دورات", emoji="📜", style=discord.ButtonStyle.primary,
                       custom_id="roulette_panel_history_btn")
    async def history_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        rows = await asyncio.to_thread(get_roulette_history_sync, interaction.guild.id, interaction.user.id)
        if not rows:
            await interaction.followup.send('ما عندك دورات محفوظة بعد؛ السجل يبدأ من هذه النسخة.', ephemeral=True)
            return
        embed = discord.Embed(title='📜 آخر 10 دورات لك', color=discord.Color.blue())
        for index, (stamp, label, reward, balance, retries) in enumerate(rows, 1):
            date = datetime.datetime.fromtimestamp(stamp, RIYADH_TZ).strftime('%Y/%m/%d • %H:%M')
            embed.add_field(name=f'{index}. {date} — بتوقيت الرياض',
                value=f'🎯 **{label}** | 💰 **+{reward} {CURRENCY_NAME}**\n'
                      f'الرصيد بعد الدورة: {balance:,} | الإعادات: {retries}', inline=False)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        apply_branding(embed, interaction.guild, user=interaction.user)
        await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="setup_roulette_panel", description="إرسال لوحة الروليت الثابتة (زر يدور بدون أمر) بروم معيّن (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="الروم المراد إرسال اللوحة فيه (اختياري، افتراضياً الروم الحالي)")
@is_owner_only()
async def setup_roulette_panel(interaction: discord.Interaction, channel: discord.TextChannel = None):
    await interaction.response.defer(ephemeral=True)
    target = channel or interaction.channel
    embed = build_roulette_panel_embed(interaction.guild)
    wheel_file = discord.File(await render_roulette_wheel_static(), filename='roulette_wheel.png')
    embed.set_image(url='attachment://roulette_wheel.png')
    try:
        await target.send(embed=embed, file=wheel_file, view=RoulettePanelView())
    finally:
        wheel_file.close()
    await interaction.followup.send(f'✅ تم إرسال لوحة الروليت الجديدة إلى {target.mention}.', ephemeral=True)

@bot.tree.command(name="pay_salaries_now", description="[صيانة] تشغيل صرف الرواتب الشهرية فوراً بدل انتظار آخر يوم بالشهر (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@is_owner_only()
async def pay_salaries_now(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    count = await pay_monthly_salaries(interaction.guild)
    today_riyadh = datetime.datetime.now(RIYADH_TZ)
    mark_salary_paid_sync(interaction.guild.id, today_riyadh.year, today_riyadh.month)
    channel_mention = f"<#{LOG_CHANNEL_ID}>"
    await interaction.followup.send(
        f"✅ تم صرف الرواتب لـ **{count}** عضو، وتم إرسال التفاصيل في {channel_mention}.\n"
        f"⚠️ ملاحظة: هذا يسجّل إن راتب هالشهر انصرف، فالمهمة التلقائية آخر الشهر ما راح تكرره.",
        ephemeral=True
    )

# 9. أمر تسجيل تاريخ الميلاد (متاح للجميع)
@bot.tree.command(name="set_birthday", description="سجّل تاريخ ميلادك عشان يهنئك البوت تلقائياً بيومك")
@app_commands.describe(day="اليوم (1-31)", month="الشهر (1-12)")
async def set_birthday(interaction: discord.Interaction, day: int, month: int):
    await interaction.response.defer(ephemeral=True)

    if not (1 <= month <= 12):
        await interaction.followup.send("❌ الشهر لازم يكون رقم بين 1 و 12.", ephemeral=True)
        return

    days_in_month = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    if not (1 <= day <= days_in_month[month - 1]):
        await interaction.followup.send(f"❌ اليوم غير صحيح لهذا الشهر (الحد الأقصى {days_in_month[month - 1]}).", ephemeral=True)
        return

    with db_lock:
        cursor.execute("""
            INSERT INTO birthdays (guild_id, user_id, month, day)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET month = ?, day = ?
        """, (interaction.guild.id, interaction.user.id, month, day, month, day))
        conn.commit()

    if not BIRTHDAY_CHANNEL_ID:
        await interaction.followup.send(
            f"✅ تم حفظ تاريخ ميلادك: **{day}/{month}**.\n"
            "⚠️ لكن لاحظ إن الإدارة لسا ما حددت روم لتهنئة أعياد الميلاد، فكلم الإداري يفعّلها.",
            ephemeral=True
        )
        return

    await interaction.followup.send(f"✅ تم حفظ تاريخ ميلادك: **{day}/{month}** — بننتظر نهنئك بيومك! 🎂", ephemeral=True)

# 10. أمر حذف تاريخ الميلاد المسجل (متاح للجميع)
@bot.tree.command(name="remove_birthday", description="حذف تاريخ ميلادك المسجل بالبوت (للأونر والنائب فقط)")
@app_commands.default_permissions(manage_guild=True)
@is_owner_or_deputy()
async def remove_birthday(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    with db_lock:
        cursor.execute("DELETE FROM birthdays WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, interaction.user.id))
        conn.commit()
    await interaction.followup.send("✅ تم حذف تاريخ ميلادك من السجل بنجاح.", ephemeral=True)

# ----------------- النظام الملكي: مراسيم رسمية + استدعاء + خطاب -----------------

# ---- منطق مشترك لإصدار مرسوم رسمي (يستخدمه أمر /decree وزر لوحة القائد والنائب) ----
async def execute_decree(interaction: discord.Interaction, title: str, content: str):
    guild = interaction.guild

    if not DECREE_CHANNEL_ID:
        await interaction.followup.send("❌ لازم تحدد DECREE_CHANNEL_ID بالكود الأول عشان تشتغل هذي الميزة.", ephemeral=True)
        return
    channel = guild.get_channel(DECREE_CHANNEL_ID)
    if not channel:
        await interaction.followup.send("❌ ما قدرت ألقى روم الإعلانات، تأكد من صحة DECREE_CHANNEL_ID.", ephemeral=True)
        return

    decree_number = get_next_decree_number(guild.id)
    decree_tag = f"{decree_number:03d}"

    embed = discord.Embed(
        title=f"📜 مرسوم رسمي رقم #{decree_tag}",
        description=f"## {title}\n\n{content}",
        color=discord.Color.from_rgb(212, 175, 55)
    )
    embed.set_author(name=f"صادر عن {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text=f"{guild.name} • مرسوم رسمي", icon_url=ICON_GIF_URL)
    embed.timestamp = discord.utils.utcnow()

    try:
        sent_msg = await channel.send(embed=embed)
    except discord.Forbidden:
        await interaction.followup.send("❌ ما يملك البوت صلاحية الإرسال بروم الإعلانات.", ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(f"❌ حدث خطأ أثناء نشر المرسوم: {e}", ephemeral=True)
        return

    try:
        await sent_msg.pin(reason=f"مرسوم رسمي رقم #{decree_tag}")
    except Exception as e:
        print(f"[Decree] تعذّر تثبيت المرسوم: {e}")

    # أرشفة المرسوم بروم اللوقز (سجل الشرف)
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        log_embed = discord.Embed(
            title=f"📜 مرسوم رسمي جديد — رقم #{decree_tag}",
            description=f"**العنوان:** {title}\n\n**المحتوى:**\n{content[:800]}",
            color=discord.Color.from_rgb(212, 175, 55)
        )
        log_embed.add_field(name="🔗 رابط المرسوم", value=f"[انتقل إلى المرسوم]({sent_msg.jump_url})", inline=False)
        apply_log_branding(log_embed, guild, target=interaction.user)
        await log_channel.send(embed=log_embed)

    await interaction.followup.send(f"✅ تم نشر المرسوم رقم **#{decree_tag}** وتثبيته بنجاح بروم {channel.mention}.", ephemeral=True)

# 11. أمر إصدار مرسوم رسمي (للأونر والنائب فقط)
@bot.tree.command(name="decree", description="إصدار مرسوم رسمي من الإدارة يُنشر ويُثبّت بروم الإعلانات (للأونر والنائب فقط)")
@app_commands.describe(title="عنوان المرسوم", content="نص المرسوم الكامل")
@app_commands.default_permissions(manage_guild=True)
@is_owner_or_deputy()
async def decree(interaction: discord.Interaction, title: str, content: str):
    await interaction.response.defer(ephemeral=True)
    await execute_decree(interaction, title, content)

# ---- منطق مشترك للاستدعاء الملكي (يستخدمه أمر /summon وزر لوحة القائد والنائب) ----
async def execute_summon(interaction: discord.Interaction, member: discord.Member, channel: discord.VoiceChannel, reason: str = None):
    if not member.voice or not member.voice.channel:
        await interaction.followup.send(f"❌ {member.mention} مو متواجد بأي روم صوتي حالياً، ما أقدر أستدعيه.", ephemeral=True)
        return

    if member.voice.channel.id == channel.id:
        await interaction.followup.send(f"⚠️ {member.mention} أصلاً متواجد بهذا الروم.", ephemeral=True)
        return

    try:
        await member.move_to(channel, reason=f"استدعاء ملكي بواسطة {interaction.user}")
    except discord.Forbidden:
        await interaction.followup.send("❌ ما يملك البوت صلاحية نقل الأعضاء بين الرومات الصوتية.", ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(f"❌ حدث خطأ أثناء الاستدعاء: {e}", ephemeral=True)
        return

    reason_text = reason or "لم يُذكر سبب"

    announce_embed = discord.Embed(
        title="👑 استدعاء ملكي",
        description=f"بأمرٍ من {interaction.user.mention}، حضر {member.mention} إلى هذا المجلس.",
        color=discord.Color.from_rgb(212, 175, 55)
    )
    announce_embed.add_field(name="🎙️ الروم الصوتي", value=channel.mention, inline=True)
    announce_embed.add_field(name="📜 السبب", value=reason_text, inline=True)
    announce_embed.set_thumbnail(url="https://i.imgur.com/KRElNgr.gif")
    apply_branding(announce_embed, interaction.guild, user=interaction.user)
    try:
        await channel.send(embed=announce_embed)
    except Exception as e:
        print(f"[Summon] تعذّر إرسال إعلان الاستدعاء: {e}")

    royal_channel = bot.get_channel(ROYAL_ANNOUNCE_CHANNEL_ID) if ROYAL_ANNOUNCE_CHANNEL_ID else None
    if royal_channel and royal_channel.id != channel.id:
        try:
            await royal_channel.send(content="@everyone", embed=announce_embed)
        except Exception as e:
            print(f"[Summon] تعذّر إرسال إعلان الاستدعاء بالروم الملكي: {e}")

    try:
        dm_embed = discord.Embed(
            title="👑 استدعاء ملكي",
            description=(
                f"وصلك استدعاء بأمرٍ من {interaction.user.mention} للحضور إلى روم **{channel.name}** في **{interaction.guild.name}**.\n\n"
                f"📜 **السبب:** {reason_text}"
            ),
            color=discord.Color.from_rgb(212, 175, 55)
        )
        dm_embed.set_image(url=BANNER_IMAGE_URL)
        await member.send(embed=dm_embed)
    except discord.Forbidden:
        pass
    except Exception as e:
        print(f"[Summon] تعذّر إرسال رسالة خاصة للعضو: {e}")

    await interaction.followup.send(f"✅ تم استدعاء {member.mention} إلى {channel.mention} بنجاح.", ephemeral=True)

# 12. أمر الاستدعاء الملكي (للأونر والنائب فقط)
@bot.tree.command(name="summon", description="استدعاء عضو متواجد بروم صوتي إلى روم صوتي آخر بإعلان رسمي (للقائد فقط)")
@app_commands.describe(member="العضو المطلوب استدعاؤه", channel="الروم الصوتي المطلوب نقله إليه", reason="سبب الاستدعاء (اختياري)")
@app_commands.default_permissions(administrator=True)
@is_owner_only()
async def summon(interaction: discord.Interaction, member: discord.Member, channel: discord.VoiceChannel, reason: str = None):
    await interaction.response.defer(ephemeral=True)
    await execute_summon(interaction, member, channel, reason)

# ---- منطق مشترك لبدء الخطاب الملكي (يستخدمه أمر /royal_address وزر لوحة القائد والنائب) ----
async def execute_royal_address_start(interaction: discord.Interaction, channel: discord.VoiceChannel, member: discord.Member) -> bool:
    if channel.id in royal_address_active:
        await interaction.followup.send("⚠️ وضع الخطاب الملكي شغّال أصلاً بهذا الروم.", ephemeral=True)
        return False

    muted_ids = []
    for m in channel.members:
        if m.bot or m.id == member.id:
            continue
        if not m.voice.mute:
            try:
                await m.edit(mute=True, reason=f"بدء الخطاب الملكي بواسطة {member}")
                muted_ids.append(m.id)
            except Exception as e:
                print(f"[Royal Address] تعذّر كتم {m}: {e}")

    royal_address_active[channel.id] = muted_ids

    embed = discord.Embed(
        title="📢 خطاب ملكي",
        description=(
            f"بدأ {member.mention} خطاباً ملكياً في هذا الروم.\n"
            "تم كتم الميكروفونات مؤقتاً للجميع، وسيتم إعادتها بعد انتهاء الخطاب."
        ),
        color=discord.Color.from_rgb(212, 175, 55)
    )
    embed.set_image(url=BANNER_IMAGE_URL)
    apply_branding(embed, interaction.guild, user=member)
    await channel.send(embed=embed)

    royal_channel = bot.get_channel(ROYAL_ANNOUNCE_CHANNEL_ID) if ROYAL_ANNOUNCE_CHANNEL_ID else None
    if royal_channel and royal_channel.id != channel.id:
        try:
            await royal_channel.send(content="@everyone", embed=embed)
        except Exception as e:
            print(f"[Royal Address] تعذّر إرسال الإعلان بالروم الملكي: {e}")

    await interaction.followup.send(f"✅ تم بدء الخطاب الملكي بروم {channel.mention}، تم كتم **{len(muted_ids)}** عضو.", ephemeral=True)
    return True

# 13. بدء وضع الخطاب الملكي (للأونر والنائب فقط)
@bot.tree.command(name="royal_address", description="بدء وضع الخطاب الملكي بالروم الصوتي الحالي: يكتم الجميع إلا أنت (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@is_owner_only()
async def royal_address(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    member = interaction.user

    if not member.voice or not member.voice.channel:
        await interaction.followup.send("❌ لازم تكون متواجد بروم صوتي عشان تبدأ الخطاب الملكي.", ephemeral=True)
        return

    channel = member.voice.channel
    await execute_royal_address_start(interaction, channel, member)

# ---- منطق مشترك لإنهاء الخطاب الملكي (يستخدمه أمر /end_address وزر لوحة القائد والنائب) ----
async def execute_royal_address_end(interaction: discord.Interaction, channel: discord.VoiceChannel, member: discord.Member) -> bool:
    muted_ids = royal_address_active.pop(channel.id, None)
    if muted_ids is None:
        await interaction.followup.send("⚠️ ماكو خطاب ملكي شغّال بهذا الروم حالياً.", ephemeral=True)
        return False

    unmuted_count = 0
    for uid in muted_ids:
        m = interaction.guild.get_member(uid)
        if m and m.voice and m.voice.channel and m.voice.channel.id == channel.id and m.voice.mute:
            try:
                await m.edit(mute=False, reason=f"انتهاء الخطاب الملكي بواسطة {member}")
                unmuted_count += 1
            except Exception as e:
                print(f"[Royal Address] تعذّر فك كتم {m}: {e}")

    embed = discord.Embed(
        title="🔚 ختام الخطاب الملكي",
        description=f"انتهى خطاب {member.mention}، وتمت إعادة الميكروفونات للجميع.",
        color=discord.Color.from_rgb(88, 101, 242)
    )
    embed.set_image(url=BANNER_IMAGE_URL)
    apply_branding(embed, interaction.guild, user=member)
    await channel.send(embed=embed)

    royal_channel = bot.get_channel(ROYAL_ANNOUNCE_CHANNEL_ID) if ROYAL_ANNOUNCE_CHANNEL_ID else None
    if royal_channel and royal_channel.id != channel.id:
        try:
            await royal_channel.send(embed=embed)
        except Exception as e:
            print(f"[Royal Address] تعذّر إرسال إعلان الختام بالروم الملكي: {e}")

    await interaction.followup.send(f"✅ تم إنهاء الخطاب الملكي، تم فك الكتم عن **{unmuted_count}** عضو.", ephemeral=True)
    return True

# 14. إنهاء وضع الخطاب الملكي (للأونر والنائب فقط)
@bot.tree.command(name="end_address", description="إنهاء وضع الخطاب الملكي وإرجاع الوضع الطبيعي للجميع (للقائد فقط)")
@app_commands.default_permissions(administrator=True)
@is_owner_only()
async def end_address(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    member = interaction.user

    if not member.voice or not member.voice.channel:
        await interaction.followup.send("❌ لازم تكون متواجد بروم صوتي عشان تنهي الخطاب الملكي.", ephemeral=True)
        return

    channel = member.voice.channel
    await execute_royal_address_end(interaction, channel, member)

# ----------------- سجلات الأحداث (LOGS) + نظام الفويس المؤقت ونظام البوست -----------------

@bot.event
async def on_invite_create(invite: discord.Invite):
    """يحدّث كاش الدعوات فور إنشاء دعوة جديدة عشان نظام تتبع الدعوات يبقى دقيق، ويسجلها بروم اللوقز."""
    invite_cache.setdefault(invite.guild.id, {})[invite.code] = {
        "uses": invite.uses or 0,
        "inviter_id": invite.inviter.id if invite.inviter else None,
        "max_uses": invite.max_uses or 0,
    }

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(title="🔗 إنشاء دعوة جديدة", color=discord.Color.green())
        embed.add_field(name="👤 أنشأها", value=invite.inviter.mention if invite.inviter else "غير معروف", inline=True)
        embed.add_field(name="📍 الروم", value=invite.channel.mention if invite.channel else "غير معروف", inline=True)
        embed.add_field(name="🔑 الكود", value=f"`{invite.code}`", inline=True)
        embed.add_field(name="🔢 الحد الأقصى للاستخدام", value=str(invite.max_uses) if invite.max_uses else "بلا حد", inline=True)
        embed.add_field(name="⏰ تنتهي", value=f"<t:{int(invite.expires_at.timestamp())}:R>" if invite.expires_at else "لا تنتهي", inline=True)
        apply_log_branding(embed, invite.guild, target=invite.inviter)
        await log_channel.send(embed=embed)

@bot.event
async def on_invite_delete(invite: discord.Invite):
    """يشيل الدعوة المحذوفة من الكاش (بعد ما نستخدمها بـ on_member_join لمعرفة آخر مستخدم لها لو كانت محدودة الاستخدام)، ويسجل حذفها بروم اللوقز."""
    guild_cache = invite_cache.get(invite.guild.id)
    cached_entry = guild_cache.pop(invite.code, None) if guild_cache else None

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        inviter_target = invite.inviter
        if not inviter_target and cached_entry and cached_entry.get("inviter_id"):
            inviter_target = invite.guild.get_member(cached_entry["inviter_id"])
        inviter_mention = inviter_target.mention if inviter_target else (
            f"<@{cached_entry['inviter_id']}>" if cached_entry and cached_entry.get("inviter_id") else "غير معروف"
        )

        embed = discord.Embed(title="🗑️ حذف دعوة", color=discord.Color.dark_gray())
        embed.add_field(name="👤 صاحبها", value=inviter_mention, inline=True)
        embed.add_field(name="📍 الروم", value=invite.channel.mention if invite.channel else "غير معروف", inline=True)
        embed.add_field(name="🔑 الكود", value=f"`{invite.code}`", inline=True)
        apply_log_branding(embed, invite.guild, target=inviter_target)
        await log_channel.send(embed=embed)

@bot.event
async def on_guild_role_create(role: discord.Role):
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return

    actor = None
    try:
        async for entry in role.guild.audit_logs(limit=5, action=discord.AuditLogAction.role_create):
            if entry.target.id == role.id:
                actor = entry.user
                break
    except Exception:
        pass

    embed = discord.Embed(title="🆕 إنشاء رتبة جديدة", color=role.color if role.color.value else discord.Color.blue())
    embed.add_field(name="🏷️ الرتبة", value=role.mention, inline=True)
    embed.add_field(name="🛡️ بواسطة", value=actor.mention if actor else "غير معروف", inline=True)
    apply_log_branding(embed, role.guild, target=actor)
    await log_channel.send(embed=embed)

@bot.event
async def on_guild_role_delete(role: discord.Role):
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return

    actor = None
    try:
        async for entry in role.guild.audit_logs(limit=5, action=discord.AuditLogAction.role_delete):
            if entry.target.id == role.id:
                actor = entry.user
                break
    except Exception:
        pass

    embed = discord.Embed(title="🗑️ حذف رتبة", color=discord.Color.dark_red())
    embed.add_field(name="🏷️ الرتبة", value=f"`@{role.name}`", inline=True)
    embed.add_field(name="🛡️ بواسطة", value=actor.mention if actor else "غير معروف", inline=True)
    apply_log_branding(embed, role.guild, target=actor)
    await log_channel.send(embed=embed)

@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    """يسجل بروم اللوقز أي تحديث بإعدادات رتبة معيّنة: الاسم، اللون، الإظهار المنفصل، الإشارة، أو الصلاحيات."""
    changes = []
    if before.name != after.name:
        changes.append(f"**الاسم:** `{before.name}` ➔ `{after.name}`")
    if before.color != after.color:
        changes.append(f"**اللون:** `{before.color}` ➔ `{after.color}`")
    if before.hoist != after.hoist:
        changes.append(f"**الإظهار المنفصل بقائمة الأعضاء:** {'مفعّل' if after.hoist else 'معطّل'}")
    if before.mentionable != after.mentionable:
        changes.append(f"**السماح بالمنشن:** {'مفعّل' if after.mentionable else 'معطّل'}")

    added_perms = [p.replace('_', ' ') for p, v in after.permissions if v and not getattr(before.permissions, p)]
    removed_perms = [p.replace('_', ' ') for p, v in before.permissions if v and not getattr(after.permissions, p)]
    if added_perms:
        changes.append(f"**صلاحيات أُضيفت:** `{', '.join(added_perms[:10])}`")
    if removed_perms:
        changes.append(f"**صلاحيات أُزيلت:** `{', '.join(removed_perms[:10])}`")

    if not changes:
        return  # تغيّر بسيط ما يهمنا تسجيله (مثل ترتيب الرتبة بالقائمة)

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return

    actor = None
    try:
        async for entry in after.guild.audit_logs(limit=5, action=discord.AuditLogAction.role_update):
            if entry.target.id == after.id:
                actor = entry.user
                break
    except Exception:
        pass

    embed = discord.Embed(title="✏️ تحديث إعدادات رتبة", color=after.color if after.color.value else discord.Color.orange())
    embed.add_field(name="🏷️ الرتبة", value=after.mention, inline=True)
    embed.add_field(name="🛡️ بواسطة", value=actor.mention if actor else "غير معروف", inline=True)
    embed.add_field(name="📋 التغييرات", value="\n".join(changes)[:1000], inline=False)
    apply_log_branding(embed, after.guild, target=actor)
    await log_channel.send(embed=embed)

@bot.event
async def on_member_join(member: discord.Member):
    role = member.guild.get_role(BOT_ROLE_ID) if member.bot else member.guild.get_role(MEMBER_ROLE_ID)
    if role:
        try:
            await member.add_roles(role)
        except Exception as e:
            print(f"Role error: {e}")

    # ----- تحديد مين دعا العضو الجديد (نظام تتبع الدعوات) -----
    inviter = None
    try:
        old_cache = invite_cache.get(member.guild.id, {})
        current_invites = await member.guild.invites()
        new_cache = {}
        for inv in current_invites:
            new_cache[inv.code] = {"uses": inv.uses or 0, "inviter_id": inv.inviter.id if inv.inviter else None, "max_uses": inv.max_uses or 0}
            old_entry = old_cache.get(inv.code)
            if old_entry and (inv.uses or 0) > old_entry["uses"] and inv.inviter:
                inviter = inv.inviter

        if inviter is None:
            # لو الدعوة محدودة الاستخدام وانحذفت تلقائياً بعد آخر استخدام، ما تظهر بالقائمة الجديدة
            # نتأكد إنها كانت على وشك الانتهاء (استخدام واحد متبقي) عشان ننسبها بثقة
            for code, old_entry in old_cache.items():
                if code not in new_cache and old_entry["max_uses"] and old_entry["uses"] + 1 >= old_entry["max_uses"] and old_entry["inviter_id"]:
                    inviter = member.guild.get_member(old_entry["inviter_id"]) or discord.Object(id=old_entry["inviter_id"])
                    break

        invite_cache[member.guild.id] = new_cache

        if inviter is not None and not member.bot and getattr(inviter, "id", None) != member.id:
            add_invite_credit(member.guild.id, inviter.id)
    except discord.Forbidden:
        pass
    except Exception as e:
        print(f"[Invite Tracker] خطأ أثناء تحديد الداعي للعضو {member}: {e}")

    if not member.bot:
        channel = bot.get_channel(WELCOME_CHANNEL_ID)
        if channel:
            image_buffer = await create_welcome_image(member)
            discord_file = discord.File(fp=image_buffer, filename="welcome.png")
            await channel.send(
                content=f"حياك الله {member.mention} في السيرفر! نورتنا 🌟",
                file=discord_file
            )

        try:
            dm_embed = discord.Embed(
                title=f"🌟 أهلاً بك في {member.guild.name}",
                description=(
                    f"يسعدنا انضمامك لنا يا {member.name} 👋\n\n"
                    "🔹 خذ جولة بالسيرفر وتعرّف على الرومات المتاحة.\n"
                    "🔹 اطّلع على القوانين والأنظمة من لوحة التحكم بالسيرفر.\n"
                    "🔹 تفاعلك بالكتابة والصوت يرفع مستواك ويفتح لك صلاحيات ومميزات جديدة.\n\n"
                    "لو احتجت أي مساعدة، تقدر تفتح تذكرة دعم فني من لوحة التحكم بالسيرفر بأي وقت. 🛠️"
                ),
                color=discord.Color.from_rgb(0, 168, 252)
            )
            apply_branding(dm_embed, member.guild)
            await send_optional_member_dm(member, member.guild.id, 'welcome', embed=dm_embed)
        except discord.Forbidden:
            pass
        except Exception as e:
            print(f"[Welcome DM] خطأ أثناء إرسال رسالة الترحيب الخاصة: {e}")

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(title="📥 انضمام عضو جديد", color=discord.Color.green())
        embed.add_field(name="👤 العضو", value=member.mention, inline=True)
        embed.add_field(name="🏷️ نوع الحساب", value="بوت" if member.bot else "مستخدم", inline=True)
        if inviter is not None:
            total_invites = get_invite_count(member.guild.id, inviter.id)
            inviter_mention = inviter.mention if isinstance(inviter, (discord.Member, discord.User)) else f"<@{inviter.id}>"
            embed.add_field(name="🔗 دعاه", value=f"{inviter_mention} (إجمالي دعواته: **{total_invites}**)", inline=False)
        embed.add_field(name="📅 تاريخ إنشاء الحساب", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=False)
        apply_log_branding(embed, member.guild, target=member)
        await log_channel.send(embed=embed)

    await update_server_stats(member.guild)

@bot.event
async def on_member_remove(member: discord.Member):
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        await asyncio.sleep(1)
        kicked = False
        actor = None
        reason = None

        try:
            async for entry in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
                if entry.target.id == member.id and (discord.utils.utcnow() - entry.created_at).total_seconds() < 10:
                    kicked = True
                    actor = entry.user
                    reason = entry.reason or "لم يُذكر سبب"
                    break
        except Exception:
            pass

        if kicked:
            log_mod_action(member.guild.id, member.id, actor.id if actor else None, "kick", reason)
            embed = discord.Embed(title="👢 طرد عضو من السيرفر (Kick)", color=discord.Color.dark_red())
            embed.add_field(name="🚫 العضو المطرود", value=f"{member.mention}\n`{member.name}`", inline=True)
            embed.add_field(name="🛡️ بواسطة الإداري", value=actor.mention if actor else "غير معروف", inline=True)
            embed.add_field(name="📝 السبب", value=f"`{reason}`", inline=False)
            apply_log_branding(embed, member.guild, target=actor if actor else member)
            await log_channel.send(embed=embed)
        else:
            embed = discord.Embed(title="🚪 مغادرة عضو", color=discord.Color.dark_gray())
            embed.add_field(name="👤 العضو", value=member.mention, inline=True)
            embed.add_field(name="👥 إجمالي الأعضاء", value=str(member.guild.member_count), inline=True)
            apply_log_branding(embed, member.guild, target=member)
            await log_channel.send(embed=embed)

    await update_server_stats(member.guild)

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return

    await asyncio.sleep(1)
    actor = None
    reason = "لم يُذكر سبب"
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban):
            if entry.target.id == user.id:
                actor = entry.user
                reason = entry.reason or "لم يُذكر سبب"
                break
    except Exception:
        pass

    log_mod_action(guild.id, user.id, actor.id if actor else None, "ban", reason)

    embed = discord.Embed(title="🔨 حظر عضو من السيرفر (Ban)", color=discord.Color.red())
    embed.add_field(name="🚫 العضو المحظور", value=f"{user.mention}\n`{user.name}`", inline=True)
    embed.add_field(name="🛡️ بواسطة الإداري", value=actor.mention if actor else "غير معروف", inline=True)
    embed.add_field(name="📝 السبب", value=f"`{reason}`", inline=False)
    apply_log_branding(embed, guild, target=actor if actor else user)
    await log_channel.send(embed=embed)

@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User):
    actor = None
    reason = "لم يُذكر سبب"
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.unban):
            if entry.target.id == user.id:
                actor = entry.user
                reason = entry.reason or "لم يُذكر سبب"
                break
    except Exception:
        pass

    log_mod_action(guild.id, user.id, actor.id if actor else None, "unban", reason)

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return
    embed = discord.Embed(title="🔓 رفع حظر عن عضو (Unban)", color=discord.Color.green())
    embed.add_field(name="👤 العضو", value=f"{user.mention}\n`{user.name}`", inline=True)
    embed.add_field(name="🛡️ بواسطة الإداري", value=actor.mention if actor else "غير معروف", inline=True)
    embed.add_field(name="📝 السبب", value=f"`{reason}`", inline=False)
    apply_log_branding(embed, guild, target=actor if actor else user)
    await log_channel.send(embed=embed)

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if not before.premium_since and after.premium_since:
        # ----- مكافأة مالية تلقائية مقابل البوست (بنفس نظام مكافآت عيد الميلاد/الفيديو) -----
        new_balance = None
        if BOOST_REWARD_AMOUNT:
            new_balance = await add_balance(after.guild.id, after.id, BOOST_REWARD_AMOUNT, reason='مكافأة تعزيز السيرفر')

        boost_channel = bot.get_channel(BOOST_CHANNEL_ID)
        if boost_channel:
            total_boosts = after.guild.premium_subscription_count
            tier = after.guild.premium_tier
            desc = (
                f"قام {after.mention} بعمل **Boost** للسيرفر، شاكرين لك هالدعم 🤍\n\n"
                f"📊 **إجمالي البوستات:** `{total_boosts}`\n"
                f"🌟 **مستوى السيرفر الحالي:** `Level {tier}`"
            )
            if BOOST_REWARD_AMOUNT:
                desc += f"\n🎁 **مكافأتك:** `+{BOOST_REWARD_AMOUNT:,} {CURRENCY_NAME}` {CURRENCY_EMOJI}"
            embed = discord.Embed(
                title="💎 شكراً لدعمك السيرفر",
                description=desc,
                color=discord.Color.from_rgb(244, 127, 255)
            )
            apply_branding(embed, after.guild, user=after)
            embed.set_thumbnail(url=after.display_avatar.url)
            try:
                await boost_channel.send(content=f"|| {after.mention} ||", embed=embed)
            except Exception as e:
                print(f"Error sending boost alert: {e}")

        log_channel_boost = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel_boost and BOOST_REWARD_AMOUNT and new_balance is not None:
            reward_log_embed = discord.Embed(title="💎 مكافأة بوست", color=discord.Color.from_rgb(244, 127, 255))
            reward_log_embed.add_field(name="👤 العضو", value=after.mention, inline=True)
            reward_log_embed.add_field(name=f"{CURRENCY_EMOJI} المكافأة", value=f"`+{BOOST_REWARD_AMOUNT:,} {CURRENCY_NAME}`", inline=True)
            reward_log_embed.add_field(name="💰 الرصيد الجديد", value=f"`{new_balance:,} {CURRENCY_NAME}`", inline=True)
            apply_log_branding(reward_log_embed, after.guild, target=after)
            await log_channel_boost.send(embed=reward_log_embed)

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return

    if before.timed_out_until != after.timed_out_until:
        if after.timed_out_until is not None and after.timed_out_until > discord.utils.utcnow():
            timeout_timestamp = int(after.timed_out_until.timestamp())
            actor = None
            reason = "لم يُذكر سبب"
            try:
                async for entry in after.guild.audit_logs(limit=5, action=discord.AuditLogAction.member_update):
                    if entry.target.id == after.id and hasattr(entry.after, 'timed_out_until') and entry.after.timed_out_until is not None:
                        actor = entry.user
                        reason = entry.reason or "لم يُذكر سبب"
                        break
            except Exception:
                pass

            log_mod_action(after.guild.id, after.id, actor.id if actor else None, "timeout", reason)

            embed = discord.Embed(title="⏳ إعطاء تايم أوت لعضو (Timeout)", color=discord.Color.dark_gold())
            embed.add_field(name="👤 العضو", value=f"{after.mention}\n`{after.name}`", inline=True)
            embed.add_field(name="🛡️ بواسطة الإداري", value=actor.mention if actor else "غير معروف", inline=True)
            embed.add_field(name="⏰ ينتهي في", value=f"<t:{timeout_timestamp}:F>\n(<t:{timeout_timestamp}:R>)", inline=False)
            embed.add_field(name="📝 السبب", value=f"`{reason}`", inline=False)
            apply_log_branding(embed, after.guild, target=actor if actor else after)
            await log_channel.send(embed=embed)
        elif before.timed_out_until is not None and (after.timed_out_until is None or after.timed_out_until <= discord.utils.utcnow()):
            actor = None
            try:
                async for entry in after.guild.audit_logs(limit=5, action=discord.AuditLogAction.member_update):
                    if entry.target.id == after.id and hasattr(entry.after, 'timed_out_until') and entry.after.timed_out_until is None:
                        actor = entry.user
                        break
            except Exception:
                pass

            log_mod_action(after.guild.id, after.id, actor.id if actor else None, "timeout_remove", None)

            embed = discord.Embed(title="✅ إزالة التايم أوت عن عضو (Timeout Removed)", color=discord.Color.green())
            embed.add_field(name="👤 العضو", value=f"{after.mention}\n`{after.name}`", inline=True)
            embed.add_field(name="🛡️ بواسطة", value=actor.mention if actor else "تلقائي / غير معروف", inline=True)
            apply_log_branding(embed, after.guild, target=actor if actor else after)
            await log_channel.send(embed=embed)

    if before.roles != after.roles:
        added_roles = [role.mention for role in after.roles if role not in before.roles]
        removed_roles = [role.mention for role in before.roles if role not in after.roles]

        role_actor = None
        try:
            async for entry in after.guild.audit_logs(limit=5, action=discord.AuditLogAction.member_role_update):
                if entry.target.id == after.id:
                    role_actor = entry.user
                    break
        except Exception:
            pass

        embed = discord.Embed(title="🛡️ تحديث رتب العضو", color=discord.Color.blue())
        embed.add_field(name="👤 العضو", value=after.mention, inline=False)
        if role_actor and role_actor.id != after.id:
            embed.add_field(name="🛠️ بواسطة", value=role_actor.mention, inline=False)
        if added_roles:
            embed.add_field(name="✅ رتب تم إضافتها", value=" ".join(added_roles), inline=False)
        if removed_roles:
            embed.add_field(name="❌ رتب تم إزالتها", value=" ".join(removed_roles), inline=False)

        apply_log_branding(embed, after.guild, target=role_actor if role_actor else after)
        await log_channel.send(embed=embed)

    if before.nick != after.nick:
        actor = None
        try:
            async for entry in after.guild.audit_logs(limit=5, action=discord.AuditLogAction.member_update):
                if entry.target.id == after.id and hasattr(entry.after, "nick"):
                    actor = entry.user
                    break
        except Exception:
            pass

        embed = discord.Embed(title="🏷️ تغيير الاسم المستعار (Nickname)", color=discord.Color.blue())
        embed.add_field(name="👤 العضو", value=after.mention, inline=True)
        embed.add_field(name="🛡️ بواسطة", value=actor.mention if actor and actor.id != after.id else after.mention, inline=True)
        embed.add_field(name="⬅️ قبل", value=f"`{before.nick or before.name}`", inline=True)
        embed.add_field(name="➡️ بعد", value=f"`{after.nick or after.name}`", inline=True)
        apply_log_branding(embed, after.guild, target=actor if actor and actor.id != after.id else after)
        await log_channel.send(embed=embed)

@bot.event
async def on_user_update(before: discord.User, after: discord.User):
    """يسجل تغيير اسم المستخدم (username) أو صورته الشخصية بروم اللوقز بكل سيرفر مشترك بينه وبين البوت."""
    if before.name == after.name and before.discriminator == after.discriminator and before.display_avatar.key == after.display_avatar.key:
        return

    for guild in bot.guilds:
        member = guild.get_member(after.id)
        if not member:
            continue
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if not log_channel:
            continue

        if before.name != after.name:
            embed = discord.Embed(title="👤 تغيير اسم المستخدم (Username)", color=discord.Color.blue())
            embed.add_field(name="⬅️ قبل", value=f"`{before.name}`", inline=True)
            embed.add_field(name="➡️ بعد", value=f"`{after.name}`", inline=True)
            apply_log_branding(embed, guild, target=after)
            await log_channel.send(embed=embed)

        if before.display_avatar.key != after.display_avatar.key:
            embed = discord.Embed(title="🖼️ تغيير الصورة الشخصية", color=discord.Color.blue())
            embed.add_field(name="👤 العضو", value=after.mention, inline=False)
            embed.set_thumbnail(url=after.display_avatar.url)
            apply_log_branding(embed, guild, target=after)
            await log_channel.send(embed=embed)

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot:
        return
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return

    embed = discord.Embed(title="🗑️ حذف رسالة", color=discord.Color.red())
    embed.add_field(name="✍️ الكاتب", value=message.author.mention, inline=True)
    embed.add_field(name="📍 القناة", value=message.channel.mention, inline=True)
    content = message.content if message.content else "*رسالة وسائط أو بدون نص*"
    embed.add_field(name="💬 المحتوى المحذوف", value=content[:1000], inline=False)
    apply_log_branding(embed, message.guild, target=message.author)
    await log_channel.send(embed=embed)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot:
        return

    # ----- كشف روابط لقطات (Medal/Streamable/Outplayed...) اتحوّلت لمعاينة بعد إرسال الرسالة -----
    # ديسكورد يضيف الـ embed بتعديل تلقائي للرسالة بعد لحظات من إرسال الرابط، مو فور الإرسال،
    # فلازم نتحقق هنا زيادة على on_message (اللي ما يشوف الـ embed أول ما تُرسل الرسالة).
    if after.guild and after.channel.id == VIDEO_REWARD_CHANNEL_ID and after.embeds:
        await handle_clip_message(after, after.author.id)

    if before.content == after.content:
        return
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return

    embed = discord.Embed(title="✏️ تعديل رسالة", color=discord.Color.orange())
    embed.add_field(name="✍️ الكاتب", value=before.author.mention, inline=True)
    embed.add_field(name="📍 القناة", value=before.channel.mention, inline=True)
    embed.add_field(name="⬅️ قبل التعديل", value=before.content[:500] or "*فارغ*", inline=False)
    embed.add_field(name="➡️ بعد التعديل", value=after.content[:500] or "*فارغ*", inline=False)
    embed.add_field(name="🔗 الرابط المباشر", value=f"[انقر هنا للانتقال للرسالة]({after.jump_url})", inline=False)
    apply_log_branding(embed, before.guild, target=before.author)
    await log_channel.send(embed=embed)

@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    is_temp_voice = channel.id in temp_channels or (isinstance(channel, discord.VoiceChannel) and channel.name.startswith("🔊・"))
    temp_channels.pop(channel.id, None)
    ticket_channels.pop(channel.id, None)
    royal_address_active.pop(channel.id, None)
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return

    actor = None
    try:
        async for entry in channel.guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_delete):
            if entry.target.id == channel.id:
                actor = entry.user
                break
    except Exception:
        pass

    if is_temp_voice:
        embed = discord.Embed(title="⏱️ تم حذف قناة صوتية مؤقتة", color=discord.Color.dark_orange())
    else:
        embed = discord.Embed(title="🗑️ حذف قناة", color=discord.Color.red())

    embed.add_field(name="📛 اسم القناة", value=f"`{channel.name}`", inline=True)
    embed.add_field(name="🏷️ النوع", value=f"`{channel.type}`", inline=True)
    embed.add_field(name="🛡️ بواسطة", value=actor.mention if actor else "غير معروف", inline=True)
    if channel.category:
        embed.add_field(name="📂 الكاتيجوري", value=channel.category.name, inline=True)
    embed.add_field(name="📍 الموضع", value=str(channel.position), inline=True)

    if not is_temp_voice:
        settings_lines = describe_channel_settings(channel)
        if settings_lines:
            embed.add_field(name="⚙️ الإعدادات (قبل الحذف)", value="\n".join(settings_lines), inline=False)
        embed.add_field(name="🔐 الصلاحيات المخصصة (قبل الحذف)", value=describe_channel_overwrites(channel), inline=False)

    apply_log_branding(embed, channel.guild, target=actor)
    await log_channel.send(embed=embed)

@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    # نتجاهل رومات الفويس المؤقتة (تتنشأ بكثرة كل ما عضو يدخل روم الإنشاء) عشان ما نغرق روم اللوقز
    if isinstance(channel, discord.VoiceChannel) and channel.name.startswith("🔊・"):
        return

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return

    actor = None
    try:
        async for entry in channel.guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_create):
            if entry.target.id == channel.id:
                actor = entry.user
                break
    except Exception:
        pass

    embed = discord.Embed(title="🆕 إنشاء قناة جديدة", color=discord.Color.green())
    embed.add_field(name="📛 الاسم", value=channel.mention if hasattr(channel, "mention") else f"`{channel.name}`", inline=True)
    embed.add_field(name="🏷️ النوع", value=f"`{channel.type}`", inline=True)
    embed.add_field(name="🛡️ بواسطة", value=actor.mention if actor else "غير معروف", inline=True)
    embed.add_field(name="📂 الكاتيجوري", value=channel.category.name if channel.category else "بدون", inline=True)
    embed.add_field(name="📍 الموضع", value=str(channel.position), inline=True)

    settings_lines = describe_channel_settings(channel)
    if settings_lines:
        embed.add_field(name="⚙️ الإعدادات", value="\n".join(settings_lines), inline=False)
    embed.add_field(name="🔐 الصلاحيات المخصصة", value=describe_channel_overwrites(channel), inline=False)

    apply_log_branding(embed, channel.guild, target=actor)
    await log_channel.send(embed=embed)

async def find_channel_update_actor(channel: discord.abc.GuildChannel):
    """يفتش بأكثر من نوع حدث بالأوديت لوق (channel_update + overwrite_create/update/delete)
    لأن ديسكورد يسجّل تعديل الصلاحيات (Permission Overwrites) تحت أنواع مختلفة عن تعديل باقي الإعدادات."""
    candidate_actions = [
        discord.AuditLogAction.channel_update,
        discord.AuditLogAction.overwrite_create,
        discord.AuditLogAction.overwrite_update,
        discord.AuditLogAction.overwrite_delete,
    ]
    best_entry = None
    for action in candidate_actions:
        try:
            async for entry in channel.guild.audit_logs(limit=5, action=action):
                if entry.target and entry.target.id == channel.id:
                    if best_entry is None or entry.created_at > best_entry.created_at:
                        best_entry = entry
                    break  # أول عنصر بكل نوع هو الأحدث (الأوديت لوق يرجع من الأحدث للأقدم)
        except Exception:
            pass
    return best_entry.user if best_entry else None

def describe_overwrites_diff(guild: discord.Guild, before_overwrites: dict, after_overwrites: dict) -> list:
    """يقارن صلاحيات القناة قبل وبعد لكل رتبة/عضو، ويرجّع سطر لكل صلاحية اتغيّرت بالضبط (سماح/منع/محايد)."""
    labels = {"True": "✅ سماح", "False": "🚫 منع", "None": "➖ محايد"}
    lines = []
    all_targets = set(before_overwrites.keys()) | set(after_overwrites.keys())
    for target in all_targets:
        before_ow = before_overwrites.get(target)
        after_ow = after_overwrites.get(target)
        if before_ow == after_ow:
            continue
        label = target.mention if hasattr(target, "mention") else f"`{getattr(target, 'name', target)}`"
        if before_ow is None:
            perm_lines = [f"`{p}`: {labels['None']} ➔ {labels[str(v)]}" for p, v in after_ow if v is not None]
            lines.append(f"➕ **صلاحية جديدة لـ** {label}\n" + "\n".join(perm_lines[:8]))
        elif after_ow is None:
            lines.append(f"➖ **حُذفت صلاحيات** {label}")
        else:
            before_map = dict(before_ow)
            after_map = dict(after_ow)
            perm_lines = [
                f"`{perm}`: {labels[str(before_map.get(perm))]} ➔ {labels[str(after_map.get(perm))]}"
                for perm in after_map
                if before_map.get(perm) != after_map.get(perm)
            ]
            if perm_lines:
                lines.append(f"🔧 **تعديل صلاحية** {label}\n" + "\n".join(perm_lines[:8]))
    return lines

@bot.event
async def on_guild_channel_update(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
    # نتجاهل رومات الفويس المؤقتة اللي المستخدمين أنفسهم يتحكمون فيها (تسمية/تحديد عدد/قفل) عبر لوحة التحكم
    if after.id in temp_channels:
        return

    # نتجاهل رومات إحصائيات السيرفر (Online/Members/Bots/In Voice) لأن البوت نفسه يعيد تسميتها
    # بشكل دوري، ولوق كل تغيير فيها يصير مزعج بدون فايدة
    STATS_CHANNEL_IDS = {STAT_ALL_MEMBERS_ID, STAT_MEMBERS_ID, STAT_BOTS_ID, STAT_ONLINE_ID, STAT_IN_VOICE_ID}
    if after.id in STATS_CHANNEL_IDS:
        return

    changes = []
    if before.name != after.name:
        changes.append(f"**الاسم:** `{before.name}` ➔ `{after.name}`")
    if isinstance(before, discord.TextChannel) and isinstance(after, discord.TextChannel):
        if before.topic != after.topic:
            changes.append(f"**الوصف (Topic):** `{before.topic or 'فارغ'}` ➔ `{after.topic or 'فارغ'}`")
        if before.nsfw != after.nsfw:
            changes.append(f"**وضع NSFW:** {'مفعّل' if after.nsfw else 'معطّل'}")
        if before.slowmode_delay != after.slowmode_delay:
            changes.append(f"**Slowmode:** `{before.slowmode_delay}s` ➔ `{after.slowmode_delay}s`")
    if isinstance(before, discord.VoiceChannel) and isinstance(after, discord.VoiceChannel):
        if before.bitrate != after.bitrate:
            changes.append(f"**البتريت:** `{before.bitrate // 1000}kbps` ➔ `{after.bitrate // 1000}kbps`")
        if before.user_limit != after.user_limit:
            changes.append(f"**الحد الأقصى للأعضاء:** `{before.user_limit or 'بلا حد'}` ➔ `{after.user_limit or 'بلا حد'}`")
    before_cat = getattr(before, "category", None)
    after_cat = getattr(after, "category", None)
    if before_cat != after_cat:
        changes.append(f"**الكاتيجوري:** `{before_cat.name if before_cat else 'بدون'}` ➔ `{after_cat.name if after_cat else 'بدون'}`")

    overwrite_lines = []
    if before.overwrites != after.overwrites:
        overwrite_lines = describe_overwrites_diff(after.guild, before.overwrites, after.overwrites)
        if not overwrite_lines:
            overwrite_lines = ["تم تعديل صلاحيات القناة (Permission Overwrites)"]

    if not changes and not overwrite_lines:
        return

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return

    actor = await find_channel_update_actor(after)

    embed = discord.Embed(title="✏️ تحديث إعدادات قناة", color=discord.Color.orange())
    embed.add_field(name="📛 القناة", value=after.mention if hasattr(after, "mention") else f"`{after.name}`", inline=True)
    embed.add_field(name="🛡️ بواسطة", value=actor.mention if actor else "غير معروف", inline=True)
    if changes:
        embed.add_field(name="📋 التغييرات العامة", value="\n".join(changes)[:1000], inline=False)
    if overwrite_lines:
        embed.add_field(name="🔐 تغييرات الصلاحيات", value="\n".join(overwrite_lines)[:1000], inline=False)
    apply_log_branding(embed, after.guild, target=actor)
    await log_channel.send(embed=embed)

@bot.event
async def on_guild_update(before: discord.Guild, after: discord.Guild):
    changes = []
    if before.name != after.name:
        changes.append(f"**اسم السيرفر:** `{before.name}` ➔ `{after.name}`")
    if before.icon != after.icon:
        changes.append("**تم تغيير أيقونة السيرفر**")
    if before.verification_level != after.verification_level:
        changes.append(f"**مستوى التحقق:** `{before.verification_level}` ➔ `{after.verification_level}`")
    if before.afk_channel != after.afk_channel:
        changes.append(f"**روم الـ AFK:** `{before.afk_channel}` ➔ `{after.afk_channel}`")

    if not changes:
        return

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return

    actor = None
    try:
        async for entry in after.audit_logs(limit=5, action=discord.AuditLogAction.guild_update):
            actor = entry.user
            break
    except Exception:
        pass

    embed = discord.Embed(title="⚙️ تحديث إعدادات السيرفر", color=discord.Color.gold())
    embed.add_field(name="🛡️ بواسطة", value=actor.mention if actor else "غير معروف", inline=True)
    embed.add_field(name="📋 التغييرات", value="\n".join(changes)[:1000], inline=False)
    if before.icon != after.icon and after.icon:
        embed.set_thumbnail(url=after.icon.url)
    apply_log_branding(embed, after, target=actor)
    await log_channel.send(embed=embed)

@bot.event
async def on_guild_emojis_update(guild: discord.Guild, before, after):
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return

    added = [e for e in after if e not in before]
    removed = [e for e in before if e not in after]
    if not added and not removed:
        return

    actor = None
    try:
        action = discord.AuditLogAction.emoji_create if added else discord.AuditLogAction.emoji_delete
        async for entry in guild.audit_logs(limit=5, action=action):
            actor = entry.user
            break
    except Exception:
        pass

    embed = discord.Embed(title="😀 تحديث الإيموجيات", color=discord.Color.purple())
    embed.add_field(name="🛡️ بواسطة", value=actor.mention if actor else "غير معروف", inline=True)
    if added:
        embed.add_field(name="✅ إيموجيات مضافة", value=" ".join(str(e) for e in added)[:1000], inline=False)
    if removed:
        embed.add_field(name="❌ إيموجيات محذوفة", value=", ".join(f"`{e.name}`" for e in removed)[:1000], inline=False)
    apply_log_branding(embed, guild, target=actor)
    await log_channel.send(embed=embed)

# ----------------- سجلات إضافية -----------------
async def send_extra_event_log(guild, title, description, target=None):
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if not guild or not channel or channel.guild.id != guild.id:
        return
    embed = discord.Embed(title=title, description=description[:3800],
                          color=discord.Color.from_rgb(50, 130, 220))
    apply_log_branding(embed, guild, target=target)
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as exc:
        print(f'[Extra Event Log] {title}: {exc}')


@bot.event
async def on_raw_message_delete(payload):
    if not payload.guild_id or payload.cached_message is not None or payload.channel_id == LOG_CHANNEL_ID:
        return
    await send_extra_event_log(bot.get_guild(payload.guild_id), '🗑️ حذف رسالة غير محفوظة بالكاش',
        f'📍 الروم: <#{payload.channel_id}>\n🆔 الرسالة: `{payload.message_id}`\n'
        'المحتوى والكاتب غير متاحين؛ الرسالة لم تكن في كاش البوت.')


@bot.event
async def on_raw_bulk_message_delete(payload):
    if not payload.guild_id or payload.channel_id == LOG_CHANNEL_ID:
        return
    examples = []
    for message in payload.cached_messages[:5]:
        text = message.content or '[مرفقات أو رسالة بلا نص]'
        examples.append(f'• {message.author.mention}: {text[:300]}')
    await send_extra_event_log(bot.get_guild(payload.guild_id), '🧹 حذف رسائل جماعي',
        f'📍 الروم: <#{payload.channel_id}>\n📊 العدد: **{len(payload.message_ids)}** رسالة\n'
        f'المحفوظ بالكاش: {len(payload.cached_messages)}\n\n'
        + ('**نماذج من الرسائل المحذوفة:**\n' + '\n'.join(examples) if examples else 'محتويات الرسائل غير متاحة في الكاش.'))


@bot.event
async def on_thread_create(thread):
    if not getattr(thread, 'newly_created', True):
        return
    await send_extra_event_log(thread.guild, '🧵 إنشاء ثريد',
        f'الثريد: {thread.mention}\nالاسم: **{thread.name}**\n'
        f'الروم الأساسي: <#{thread.parent_id}>\nصاحب الثريد: <@{thread.owner_id}>')


@bot.event
async def on_thread_delete(thread):
    await send_extra_event_log(thread.guild, '🗑️ حذف ثريد',
        f'الاسم: **{thread.name}**\nالآيدي: `{thread.id}`\nالروم الأساسي: <#{thread.parent_id}>')


@bot.event
async def on_raw_thread_delete(payload):
    if getattr(payload, 'thread', None) is not None:
        return  # الثريد المحفوظ بالكاش يسجله الحدث العادي، دون تكرار.
    await send_extra_event_log(bot.get_guild(payload.guild_id), '🗑️ حذف ثريد غير محفوظ بالكاش',
        f'آيدي الثريد: `{payload.thread_id}`\nالروم الأساسي: <#{payload.parent_id}>')


@bot.event
async def on_thread_update(before, after):
    changes = []
    for attribute, label in [('name', 'الاسم'), ('archived', 'الأرشفة'), ('locked', 'القفل'),
                             ('slowmode_delay', 'مهلة الكتابة'), ('auto_archive_duration', 'مدة الأرشفة التلقائية')]:
        old, new = getattr(before, attribute), getattr(after, attribute)
        if old != new:
            changes.append(f'• **{label}:** `{old}` ← `{new}`')
    if changes:
        await send_extra_event_log(after.guild, '🧵 تعديل ثريد', after.mention + '\n' + '\n'.join(changes))


def scheduled_event_summary(event):
    start = f'<t:{int(event.start_time.timestamp())}:F>' if event.start_time else 'غير محدد'
    location = getattr(event, 'location', None) or (event.channel.mention if event.channel else 'غير محدد')
    return f'**{event.name}**\n🆔 `{event.id}`\n🕒 البداية: {start}\n📍 المكان: {location}\n📌 الحالة: {event.status}'


@bot.event
async def on_scheduled_event_create(event):
    await send_extra_event_log(event.guild, '📅 إنشاء فعالية', scheduled_event_summary(event))


@bot.event
async def on_scheduled_event_delete(event):
    await send_extra_event_log(event.guild, '🗑️ حذف فعالية', scheduled_event_summary(event))


@bot.event
async def on_scheduled_event_update(before, after):
    changes = []
    for attribute, label in [('name', 'الاسم'), ('description', 'الوصف'), ('start_time', 'البداية'),
                             ('end_time', 'النهاية'), ('status', 'الحالة'), ('location', 'المكان')]:
        old, new = getattr(before, attribute, None), getattr(after, attribute, None)
        if old != new:
            changes.append(f'• **{label}:** {str(old)[:400]} ← {str(new)[:400]}')
    if getattr(before, 'channel_id', None) != getattr(after, 'channel_id', None):
        changes.append(f'• روم الفعالية: `{before.channel_id}` ← `{after.channel_id}`')
    if changes:
        await send_extra_event_log(after.guild, '📅 تحديث فعالية', scheduled_event_summary(after) + '\n\n' + '\n'.join(changes))


@bot.event
async def on_guild_stickers_update(guild, before, after):
    old = {sticker.id: sticker for sticker in before}
    new = {sticker.id: sticker for sticker in after}
    changes = []
    for sticker_id in new.keys() - old.keys():
        changes.append(f'➕ إضافة **{new[sticker_id].name}** (`{sticker_id}`)')
    for sticker_id in old.keys() - new.keys():
        changes.append(f'➖ حذف **{old[sticker_id].name}** (`{sticker_id}`)')
    for sticker_id in old.keys() & new.keys():
        if any(getattr(old[sticker_id], key, None) != getattr(new[sticker_id], key, None)
               for key in ('name', 'description', 'emoji', 'available')):
            changes.append(f'✏️ تعديل **{old[sticker_id].name}** ← **{new[sticker_id].name}** (`{sticker_id}`)')
    if changes:
        await send_extra_event_log(guild, '🎨 تحديث ستكرات السيرفر', '\n'.join(changes))


async def log_extra_voice_changes(member, before, after):
    changes = []
    for attribute, enabled, disabled in [
        ('self_video', '📷 فتح الكاميرا', '📷 إغلاق الكاميرا'),
        ('mute', '🔇 كتم إداري للمايك', '🎙️ إزالة الكتم الإداري'),
        ('deaf', '🔕 كتم إداري للسماعات', '🔊 إزالة كتم السماعات الإداري'),
        ('suppress', '🎭 نقله للمستمعين في الستيج', '🎭 السماح له بالتحدث في الستيج'),
    ]:
        if attribute != 'self_video' and (before.channel is None or after.channel is None):
            continue  # لا نفسّر تصفير حالة العضو عند الخروج كإزالة كتم إداري.
        if getattr(before, attribute, False) != getattr(after, attribute, False):
            changes.append(enabled if getattr(after, attribute, False) else disabled)
    if changes:
        channel = after.channel or before.channel
        location = channel.mention if channel else 'غير معروف'
        await send_extra_event_log(member.guild, '🎙️ تغير حالة الصوت',
            f'العضو: {member.mention}\nالروم: {location}\n' + '\n'.join(changes), target=member)


cursor.execute('''CREATE TABLE IF NOT EXISTS game_activity_sessions (
    guild_id INTEGER, user_id INTEGER, game_key TEXT, game_name TEXT,
    started_ts REAL, approximate INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, game_key)
)''')
conn.commit()


with db_lock:
    if not conn.execute("SELECT 1 FROM bot_state WHERE key='game_voice_only_v1'").fetchone():
        conn.execute('DELETE FROM game_activity_sessions')
        conn.execute("INSERT INTO bot_state(key,value) VALUES('game_voice_only_v1','done')")
        conn.commit()


def get_playing_games(member, now):
    games = {}
    for activity in member.activities:
        if activity.type != discord.ActivityType.playing or not activity.name:
            continue
        name = activity.name.strip()
        if not name:
            continue
        start = getattr(activity, 'start', None)
        started_ts = start.timestamp() if isinstance(start, datetime.datetime) else None
        if started_ts is not None and (not math.isfinite(started_ts) or started_ts < 0 or started_ts > now):
            started_ts = None
        games[name.casefold()] = (name, started_ts)
    return games


def seed_game_activity_sync(snapshots, now):
    with db_lock:
        for guild_id, user_id, games in snapshots:
            for key, (name, client_start) in games.items():
                conn.execute('INSERT OR IGNORE INTO game_activity_sessions VALUES (?, ?, ?, ?, ?, ?)',
                             (guild_id, user_id, key, name, client_start if client_start is not None else now,
                              int(client_start is None)))
                if client_start is not None:
                    conn.execute('UPDATE game_activity_sessions SET started_ts=?, approximate=0 '
                                 'WHERE guild_id=? AND user_id=? AND game_key=?',
                                 (client_start, guild_id, user_id, key))
        conn.commit()


def track_game_activity_sync(guild_id, user_id, before_games, after_games, now):
    events = []
    with db_lock:
        rows = {row[0]: row[1:] for row in conn.execute(
            'SELECT game_key, game_name, started_ts, approximate FROM game_activity_sessions '
            'WHERE guild_id=? AND user_id=?', (guild_id, user_id)
        ).fetchall()}
        for key in set(before_games) | set(rows):
            if key in after_games:
                continue
            if key in before_games:
                name, client_start = before_games[key]
                stored = rows.get(key)
                started_ts = stored[1] if stored else client_start
                approximate = bool(stored[2]) if stored else started_ts is None
                events.append(('end', name, started_ts, approximate))
            # الحالة القديمة غير الموجودة في كاش ديسكورد تُزال دون اختلاق مدة انتهائها.
            conn.execute('DELETE FROM game_activity_sessions WHERE guild_id=? AND user_id=? AND game_key=?',
                         (guild_id, user_id, key))
        for key, (name, client_start) in after_games.items():
            if key not in before_games:
                started_ts = client_start if client_start is not None else now
                approximate = client_start is None
                conn.execute('INSERT OR REPLACE INTO game_activity_sessions VALUES (?, ?, ?, ?, ?, ?)',
                             (guild_id, user_id, key, name, started_ts, int(approximate)))
                events.append(('start', name, started_ts, approximate))
            elif key not in rows:
                conn.execute('INSERT INTO game_activity_sessions VALUES (?, ?, ?, ?, ?, ?)',
                             (guild_id, user_id, key, name, client_start if client_start is not None else now,
                              int(client_start is None)))
        conn.commit()
    return events


async def process_voice_game_change(member, before_games, after_games, now, voice_channel):
    # نبدأ الحساب من رصد اللعبة داخل الفويس، دون احتساب اللعب السابق خارج الروم.
    before_games = {key: (name, None) for key, (name, start) in before_games.items()}
    after_games = {key: (name, None) for key, (name, start) in after_games.items()}
    try:
        events = await asyncio.to_thread(track_game_activity_sync, member.guild.id, member.id,
                                         before_games, after_games, now)
        channel = bot.get_channel(LOG_CHANNEL_ID)
        if not channel or channel.guild.id != member.guild.id:
            return
        for event, name, started_ts, approximate in events:
            embed = discord.Embed(title='🎮 بدأ يلعب داخل الفويس' if event == 'start' else '🏁 انتهاء رصد اللعبة في الفويس',
                                  color=discord.Color.green() if event == 'start' else discord.Color.dark_grey())
            embed.add_field(name='👤 العضو', value=member.mention, inline=True)
            embed.add_field(name='🎮 اللعبة', value=name[:1024], inline=True)
            embed.add_field(name='🎙️ الروم', value=voice_channel.mention, inline=False)
            if started_ts is not None:
                embed.add_field(name='🕒 بداية الرصد', value=f'<t:{int(started_ts)}:F>', inline=False)
            if event == 'end':
                duration = format_live_stream_duration(now - started_ts) if started_ts is not None else 'غير معروفة؛ لم تُرصد البداية'
                embed.add_field(name='⏱️ مدة رصد اللعب داخل الفويس', value=duration, inline=False)
                embed.set_footer(text='حسب نشاط اللعبة الظاهر بالديسكورد أثناء وجود العضو في روم صوتي.')
            apply_log_branding(embed, member.guild, target=member)
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except Exception as exc:
        print(f'[Voice Game Activity] {member.id}: {exc}')


@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    if after.bot or not after.voice or not after.voice.channel:
        return
    now = time.time()
    before_games = get_playing_games(before, now)
    after_games = get_playing_games(after, now)
    if before_games or after_games:
        await process_voice_game_change(after, before_games, after_games, now, after.voice.channel)


cursor.execute('''CREATE TABLE IF NOT EXISTS live_stream_sessions (
    guild_id INTEGER, user_id INTEGER, started_ts REAL NOT NULL,
    channel_id INTEGER, approximate INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
)''')
conn.commit()


def track_live_stream_sync(guild_id, user_id, before_channel, before_stream,
                           after_channel, after_stream, now):
    was_live = bool(before_channel and before_stream)
    is_live = bool(after_channel and after_stream)
    if not was_live and not is_live:
        return None
    with db_lock:
        if is_live and not was_live:
            conn.execute('INSERT OR REPLACE INTO live_stream_sessions VALUES (?, ?, ?, ?, 0)',
                         (guild_id, user_id, now, after_channel))
            conn.commit()
            return ('start', now, after_channel, False)
        if was_live and not is_live:
            row = conn.execute('SELECT started_ts, channel_id, approximate FROM live_stream_sessions '
                               'WHERE guild_id=? AND user_id=?', (guild_id, user_id)).fetchone()
            conn.execute('DELETE FROM live_stream_sessions WHERE guild_id=? AND user_id=?', (guild_id, user_id))
            conn.commit()
            return ('end', row[0] if row else None, before_channel, bool(row[2]) if row else True)
        if is_live:
            # الانتقال لروم آخر أو تغيير حالة المايك لا يصفر مدة البث المتواصل.
            conn.execute('INSERT OR IGNORE INTO live_stream_sessions VALUES (?, ?, ?, ?, 1)',
                         (guild_id, user_id, now, after_channel))
            conn.execute('UPDATE live_stream_sessions SET channel_id=? WHERE guild_id=? AND user_id=?',
                         (after_channel, guild_id, user_id))
            conn.commit()
    return None


def format_live_stream_duration(seconds):
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours} ساعة و{minutes} دقيقة و{seconds} ثانية' if hours else f'{minutes} دقيقة و{seconds} ثانية'


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    guild = member.guild
    live_event_ts = time.time()
    await log_extra_voice_changes(member, before, after)
    if not member.bot:
        games = get_playing_games(member, live_event_ts)
        if before.channel is None and after.channel is not None and games:
            await process_voice_game_change(member, {}, games, live_event_ts, after.channel)
        elif before.channel is not None and after.channel is None:
            await process_voice_game_change(member, games, {}, live_event_ts, before.channel)
    live_event = await asyncio.to_thread(
        track_live_stream_sync, guild.id, member.id,
        before.channel.id if before.channel else None, before.self_stream,
        after.channel.id if after.channel else None, after.self_stream, live_event_ts
    )

    # ----- تسجيل الحضور اليومي عند دخول أي روم صوتي (مرة وحدة باليوم لكل عضو) -----
    if before.channel is None and after.channel is not None:
        asyncio.create_task(process_daily_checkin(member))

    if after.channel and after.channel.id in TEMP_VOICE_CREATOR_IDS:
        category = after.channel.category
        new_channel_name = f"🔊・{member.display_name}"

        try:
            # نحدد صراحة إن الجميع يقدر يشوف/يدخل/يتكلم بالروم الجديد
            # حتى لو الكاتيجوري نفسها فيها قيود (مثلاً روم الإنشاء نفسه ممنوع فيه الكلام)
            temp_overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
            }
            new_voice_channel = await guild.create_voice_channel(
                name=new_channel_name,
                category=category,
                overwrites=temp_overwrites,
                reason=f"Temp voice created by {member.display_name}"
            )

            temp_channels[new_voice_channel.id] = member.id
            await member.move_to(new_voice_channel)
            await asyncio.sleep(1)

            if guild.get_channel(new_voice_channel.id):
                embed = discord.Embed(
                    title="واجهة التحكم الصوتية",
                    description=(
                        f"**مالك القناة:** {member.mention}\n"
                        "يمكن استخدام هذه **الواجهة** لإدارة قناتك الصوتية المؤقتة.\n\n"
                        "**اضغط الأزرار أدناه لاستخدام الواجهة، أو زر ❓ مساعدة لشرح تفصيلي بالصور:**"
                    ),
                    color=discord.Color.from_rgb(0, 168, 252)
                )
                apply_branding(embed, guild, user=member)

                # إيمبيد إضافي لعرض صورة GIF تزيينية فوق واجهة الشرح
                decor_embed = discord.Embed(color=discord.Color.from_rgb(0, 168, 252))
                decor_embed.set_image(url="https://i.imgur.com/pgi8UCX.gif")

                control_view = TempVoiceControlView(new_voice_channel, member.id)
                control_view.base_embed = embed
                control_view.decor_embed = decor_embed

                panel_msg = await new_voice_channel.send(content=member.mention, embeds=[decor_embed, embed], view=control_view)
                control_view.status_message = panel_msg
        except Exception as e:
            print(f"Error creating temp voice: {e}")

    if before.channel:
        is_temp_by_id = before.channel.id in temp_channels
        is_temp_by_name = before.channel.name.startswith("🔊・")
        
        if (is_temp_by_id or is_temp_by_name) and len(before.channel.members) == 0:
            temp_channels.pop(before.channel.id, None)
            try:
                await before.channel.delete(reason="Temp voice empty")
            except Exception as e:
                print(f"Error deleting temp channel: {e}")

    if before.channel and (after.channel is None or before.channel.id != after.channel.id):
        old_msg = vip_voice_messages.pop(member.id, None)
        if old_msg:
            try:
                await old_msg.delete()
            except Exception:
                pass

    if after.channel and after.channel.id not in TEMP_VOICE_CREATOR_IDS and (before.channel is None or before.channel.id != after.channel.id):
        is_owner = (member.id == guild.owner_id)
        is_deputy = (DEPUTY_ROLE_ID != 0 and any(r.id == DEPUTY_ROLE_ID for r in member.roles)) or any(
            rid != 0 and any(r.id == rid for r in member.roles) for rid in EXTRA_STAFF_ROLE_IDS
        )

        if is_owner:
            other_members = [m for m in after.channel.members if m.id != member.id and not m.bot]
            if other_members:
                async def mute_routine():
                    muted_list = []
                    for m in other_members:
                        if not m.voice.mute:
                            try:
                                await m.edit(mute=True, reason="VIP Owner Arrival Mute")
                                muted_list.append(m)
                            except Exception:
                                pass
                    if muted_list:
                        await asyncio.sleep(0.5)
                        for m in muted_list:
                            try:
                                await m.edit(mute=False, reason="VIP Owner Arrival Unmute")
                            except Exception:
                                pass
                asyncio.create_task(mute_routine())

        now = time.time()
        last_vip_alert = vip_voice_cooldowns.get(member.id, 0)

        if now - last_vip_alert >= 60:
            if is_owner or is_deputy:
                vip_voice_cooldowns[member.id] = now
                if is_owner:
                    vip_embed = discord.Embed(
                        title="👑 صاحب السيرفر متواجد الآن",
                        description=(
                            f"أهلاً بك {member.mention}، تشرّفنا بحضورك.\n\n"
                            f"🎙️ **الروم الصوتي:** `{after.channel.name}`"
                        ),
                        color=discord.Color.from_rgb(255, 215, 0)
                    )
                else:
                    vip_embed = discord.Embed(
                        title="🛡️ نائب الإدارة متواجد الآن",
                        description=(
                            f"أهلاً بك {member.mention}، نورت الروم.\n\n"
                            f"🎙️ **الروم الصوتي:** `{after.channel.name}`"
                        ),
                        color=discord.Color.from_rgb(230, 126, 34)
                    )
                
                apply_branding(vip_embed, guild, user=member)
                try:
                    sent_msg = await after.channel.send(content=f"|| {member.mention} ||", embed=vip_embed)
                    vip_voice_messages[member.id] = sent_msg
                except Exception as e:
                    print(f"Error sending VIP voice welcome: {e}")

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        if before.channel is None and after.channel is not None:
            voice_timers[member.id] = time.time()
            embed = discord.Embed(title="🔊 دخول روم صوتي", color=discord.Color.green())
            embed.add_field(name="👤 العضو", value=member.mention, inline=True)
            embed.add_field(name="🎙️ الروم", value=after.channel.name, inline=True)
            apply_log_branding(embed, member.guild, target=member)
            await log_channel.send(embed=embed)

        elif before.channel is not None and after.channel is None:
            duration_str = "غير معروف"
            if member.id in voice_timers:
                elapsed = int(time.time() - voice_timers.pop(member.id))
                m, s = divmod(elapsed, 60)
                h, m = divmod(m, 60)
                duration_str = f"{h} ساعة و {m} دقيقة" if h > 0 else f"{m} دقيقة و {s} ثانية"

            embed = discord.Embed(title="🔇 خروج من روم صوتي", color=discord.Color.red())
            embed.add_field(name="👤 العضو", value=member.mention, inline=True)
            embed.add_field(name="🎙️ الروم", value=before.channel.name, inline=True)
            embed.add_field(name="⏱️ مدة الجلوس", value=f"`{duration_str}`", inline=False)
            apply_log_branding(embed, member.guild, target=member)
            await log_channel.send(embed=embed)

        elif before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
            embed = discord.Embed(title="🔀 انتقال بين رومات صوتية", color=discord.Color.blue())
            embed.add_field(name="👤 العضو", value=member.mention, inline=False)
            embed.add_field(name="⬅️ من روم", value=f"🔊 {before.channel.name}", inline=True)
            embed.add_field(name="➡️ إلى روم", value=f"🔊 {after.channel.name}", inline=True)
            apply_log_branding(embed, member.guild, target=member)
            await log_channel.send(embed=embed)

        # ----- تسجيل بداية/نهاية مشاركة الشاشة (Screen Share) -----
        if live_event and live_event[0] == 'start':
            embed = discord.Embed(title="🔴 بدء بث Live", color=discord.Color.green())
            embed.add_field(name="👤 العضو", value=member.mention, inline=True)
            embed.add_field(name="🎙️ الروم", value=after.channel.name, inline=True)
            apply_log_branding(embed, member.guild, target=member)
            await log_channel.send(embed=embed)
        elif live_event and live_event[0] == 'end':
            share_channel = after.channel or before.channel
            embed = discord.Embed(title="⏹️ انتهاء بث Live", color=discord.Color.dark_grey())
            embed.add_field(name="👤 العضو", value=member.mention, inline=True)
            if share_channel:
                embed.add_field(name="🎙️ الروم", value=share_channel.name, inline=True)
            started_ts = live_event[1]
            duration = format_live_stream_duration(live_event_ts - started_ts) if started_ts is not None else 'غير معروفة؛ لم يرصد البوت بداية البث'
            if started_ts is not None and live_event[3]:
                duration += ' (من أول رصد للبث)'
            embed.add_field(name="⏱️ مدة البث", value=duration, inline=False)
            embed.add_field(name="📌 سبب الانتهاء", value='الخروج من الروم' if after.channel is None else 'إغلاق البث', inline=True)
            apply_log_branding(embed, member.guild, target=member)
            await log_channel.send(embed=embed)

        # ----- تتبع سكوت المايك (self mute) المتواصل عشان اكتشاف AFK -----
        # الشروط: لازم يكون بفويس، مو بروم مستثنى، مايكه مسكر (self mute)، ومو مسوي شير سكرين
        should_track_mute = (
            after.channel is not None
            and after.channel.id in AFK_TRACKED_CHANNEL_IDS
            and after.channel.id not in AFK_EXCLUDED_CHANNEL_IDS
            and after.self_mute
            and not after.self_stream
        )
        if should_track_mute:
            if member.id not in mute_start_times:
                mute_start_times[member.id] = time.time()
        else:
            # خرج، أو بروم مستثنى، أو فاتح مايكه، أو يسوي شير سكرين: صفّر التتبع
            mute_start_times.pop(member.id, None)
            afk_logged_members.discard(member.id)

    if before.channel != after.channel:
        await update_voice_stat_direct(member.guild)

async def update_voice_stat_direct(guild: discord.Guild):
    if not guild:
        return
    in_voice_count = sum(len(vc.members) for vc in guild.voice_channels)
    await update_single_stat(guild, STAT_IN_VOICE_ID, f"In Voice: {in_voice_count}")

def _handle_bot_termination(signum,frame):
    # SystemExit يفك الأقفال ثم يترك finally يحفظ نسخة الإغلاق.
    raise SystemExit(0)

# Extra protection outside discord.py's own per-route rate limiter.
# Never retry permission/authentication errors, or uncertain failed writes.
_ready_initialized = False
_ready_initialization_lock = asyncio.Lock()
_discord_request_gate = asyncio.Lock()
_discord_next_request = 0.0
_discord_pause_until = float(get_bot_state("discord_pause_until") or 0)
_original_discord_request = bot.http.request


def discord_retry_delay(exc):
    headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
    delays = []
    for key in ("Retry-After", "X-RateLimit-Reset-After"):
        try:
            value = float(headers.get(key, 0))
            if math.isfinite(value) and value > 0:
                delays.append(value)
        except (TypeError, ValueError):
            pass
    # HTML 429 has no usable bucket information: wait conservatively.
    return max(delays) if delays else 3600.0


async def guarded_discord_request(route, **kwargs):
    global _discord_pause_until, _discord_next_request
    route_key = (route.method, route.path, str(getattr(route, "channel_id", None)), str(getattr(route, "guild_id", None)))
    while True:
        # A single gate coalesces concurrent callers and rechecks any new pause.
        async with _discord_request_gate:
            cached = _permission_blocks.get(route_key)
            if cached and cached[0] > time.monotonic():
                _request_metrics["suppressed_403"] += 1
                raise discord.Forbidden(cached[1], {"code": 50013, "message": "Permission requests paused for this route for 15 minutes."})
            _permission_blocks.pop(route_key, None)
            while _discord_pause_until > time.time():
                await asyncio.sleep(min(30.0, _discord_pause_until - time.time()))
            delay = _discord_next_request - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            _discord_next_request = time.monotonic() + 0.2
            try:
                _request_metrics["requests"] += 1
                metric_key = (route.method, route.path)
                _route_metrics[metric_key] = _route_metrics.get(metric_key, 0) + 1
                return await _original_discord_request(route, **kwargs)
            except discord.HTTPException as exc:
                _request_metrics["errors"] += 1
                if exc.status == 403:
                    _permission_blocks[route_key] = (time.monotonic() + 900, exc.response)
                    print(f"[Discord Safety] HTTP 403 on {route.method} {route.path}: requests suspended for this resource for 15 minutes.")
                if exc.status != 429:
                    raise
                _request_metrics["429"] += 1
                delay = discord_retry_delay(exc)
                _discord_pause_until = max(_discord_pause_until, time.time() + delay)
                # Survives host restarts; never print the HTML page or credentials.
                set_bot_state("discord_pause_until", str(_discord_pause_until))
                print(f"[Discord Safety] HTTP 429: API requests paused for {delay:.0f}s. Keep the process running.")
                # Multipart streams may have been consumed. Do not replay them.
                if kwargs.get("files"):
                    raise discord.HTTPException(exc.response, {"code": 0, "message": "Rate limited; upload was not retried."}) from None


_permission_blocks = {}
_request_metrics = {"requests": 0, "errors": 0, "429": 0, "suppressed_403": 0}
_route_metrics = {}


@tasks.loop(minutes=1)
async def request_monitor_task():
    top = sorted(_route_metrics.items(), key=lambda pair: pair[1], reverse=True)[:3]
    print(f"[API Monitor] last minute: {_request_metrics}; busiest routes: {top}")
    for key in _request_metrics:
        _request_metrics[key] = 0
    _route_metrics.clear()
    now = time.monotonic()
    for key, (until, _) in list(_permission_blocks.items()):
        if until <= now:
            _permission_blocks.pop(key, None)


def acquire_instance_lock():
    # OS releases the lock even after a crash; do not rely on a stale PID file.
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.bot_instance.lock')
    handle = open(path, 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SystemExit('[Discord Safety] Another bot instance is already running in this directory.')
    return handle


def release_instance_lock(handle):
    if not handle.closed:
        handle.close()


_original_message_send = discord.abc.Messageable.send
_original_message_edit = discord.Message.edit
_log_batch_queue = []
_panel_edit_queue = {}
_background_write_tasks = set()
_shutting_down_writes = False


def track_write_task(coro):
    task = asyncio.create_task(coro)
    _background_write_tasks.add(task)
    task.add_done_callback(_background_write_tasks.discard)
    return task


async def flush_log_batch():
    await asyncio.sleep(2)
    # Each send keeps Discord's limits: <=10 embeds, <=6000 embed characters.
    while _log_batch_queue:
        batch = []
        contents = []
        embeds = []
        chars = 0
        while _log_batch_queue:
            channel, content, new_embeds, future = _log_batch_queue[0]
            new_content = '\n'.join(contents + ([content] if content else []))
            new_chars = sum(len(embed) for embed in new_embeds)
            if batch and (len(new_content) > 2000 or len(embeds) + len(new_embeds) > 10 or chars + new_chars > 6000):
                break
            _log_batch_queue.pop(0)
            batch.append((channel, future))
            if content:
                contents.append(content)
            embeds.extend(new_embeds)
            chars += new_chars
        try:
            result = await _original_message_send(batch[0][0], '\n'.join(contents) or None,
                                                  embeds=embeds, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            for _, future in batch:
                if not future.done():
                    future.set_exception(exc)
        else:
            for _, future in batch:
                if not future.done():
                    future.set_result(result)


async def batched_log_send(channel, content=None, **kwargs):
    if (_shutting_down_writes or getattr(channel, 'id', None) != LOG_CHANNEL_ID
            or set(kwargs) - {'embed', 'embeds', 'allowed_mentions'}):
        return await _original_message_send(channel, content, **kwargs)
    if kwargs.get('embed') is not None and kwargs.get('embeds') is not None:
        return await _original_message_send(channel, content, **kwargs)
    embeds = list(kwargs.get('embeds') or [])
    if kwargs.get('embed') is not None:
        embeds.append(kwargs['embed'])
    content = str(content) if content is not None else ''
    if (not content and not embeds) or len(content) > 2000 or len(embeds) > 10 or sum(len(e) for e in embeds) > 6000:
        return await _original_message_send(channel, content or None, **kwargs)
    # Backpressure bounds memory without dropping audit events.
    if len(_log_batch_queue) >= 100:
        return await _original_message_send(channel, content or None, **kwargs)
    future = asyncio.get_running_loop().create_future()
    start = not _log_batch_queue
    _log_batch_queue.append((channel, content, embeds, future))
    if start:
        track_write_task(flush_log_batch())
    return await asyncio.shield(future)


async def flush_panel_edit(key):
    await asyncio.sleep(0.5)
    message, changes, futures = _panel_edit_queue.pop(key)
    try:
        result = await _original_message_edit(message, **changes)
    except Exception as exc:
        for future in futures:
            if not future.done():
                future.set_exception(exc)
    else:
        for future in futures:
            if not future.done():
                future.set_result(result)


async def coalesced_panel_edit(message, **kwargs):
    # Files and lifecycle changes keep their original immediate semantics.
    if (_shutting_down_writes or not kwargs or set(kwargs) - {'content', 'embed', 'embeds', 'view', 'allowed_mentions'}
            or ('embed' in kwargs and 'embeds' in kwargs)):
        return await _original_message_edit(message, **kwargs)
    key = (message.channel.id, message.id)
    future = asyncio.get_running_loop().create_future()
    if key not in _panel_edit_queue:
        _panel_edit_queue[key] = (message, {}, [])
        track_write_task(flush_panel_edit(key))
    _, changes, futures = _panel_edit_queue[key]
    if 'embed' in kwargs:
        changes.pop('embeds', None)
    if 'embeds' in kwargs:
        changes.pop('embed', None)
    changes.update(kwargs)
    futures.append(future)
    return await asyncio.shield(future)


_original_bot_close = bot.close


async def close_with_pending_writes():
    global _shutting_down_writes
    _shutting_down_writes = True
    pending = list(_background_write_tasks)
    if pending:
        # Flush before the Discord HTTP session closes, without hanging shutdown.
        _, unfinished = await asyncio.wait(pending, timeout=5)
        if unfinished:
            print('[Discord Safety] Shutdown timeout: some writes remain pending.')
            for task in unfinished:
                task.cancel()
    await _original_bot_close()


bot.close = close_with_pending_writes
discord.abc.Messageable.send = batched_log_send
discord.Message.edit = coalesced_panel_edit

bot.http.request = guarded_discord_request

if __name__ == '__main__':
    import signal
    _instance_lock_handle = acquire_instance_lock()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _handle_bot_termination)

    def global_exception_logger(exc_type, exc_value, exc_traceback):
        if exc_type is KeyboardInterrupt:
            return
        print("❌ UNHANDLED ERROR:")
        traceback.print_exception(exc_type, exc_value, exc_traceback)

    sys.excepthook = global_exception_logger

    try:
        print("🚀 Starting Discord bot...")
        bot.run(BOT_TOKEN)
    except Exception:
        print("❌ BOT RUN ERROR")
        traceback.print_exc()
    finally:
        try:
            path = create_safety_backup_sync('نسخة عند إغلاق البوت', backup_command_manifest())
            print(f'[Safety Backup] نسخة الإغلاق محفوظة: {path}')
        except Exception as exc:
            print(f'[Safety Backup] تعذر حفظ نسخة الإغلاق: {exc}')
        release_instance_lock(_instance_lock_handle)
