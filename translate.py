"""第二層：策展與翻譯（透過 Claude Code CLI，不需要 API key）。

防編造的核心設計 —— 靠結構，不靠模型自律：

  1. 模型**永遠不輸出網址**。它只輸出項目 id 與譯文，網址由程式依 id
     從 candidates.json join 回來。模型在結構上沒有機會捏造連結。
  2. 模型只能改寫「眼前的文字」（title_original / summary_original），
     不得補充背景知識。抓不到摘要就標「僅標題」，不准腦補內文。
  3. 輸出的每個 id 都必須存在於輸入，對不上就整批重跑，不是警告後放行。
  4. 譯文若出現 http/https 字樣一律判定失敗（模型試圖生成連結）。
  5. 數字、金額、模型版本號要求保留原文寫法。

CLI 沒有 json_schema 可強制結構化輸出，改以三道防線取代：
    llm.extract_json  寬容解析（容忍圍欄與開場白）
    _shape_*          結構檢查，不合就把錯誤回饋給模型重試
    verify            語意檢查，對照輸入驗證，兩次不過就中止

流程：
    第一階段 策展：合併跨語言重複、剔除無實質內容者、分組排序（只輸出 id）
    第二階段 翻譯：分批譯成繁體中文（只輸出 id + 譯文）

用法：
    python translate.py                 # 需先用 claude 登入過
    python translate.py --limit 6       # 只處理前 6 則，試跑用
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import llm
from llm import LLMError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
OUT = ROOT / "out"
CANDIDATES = OUT / "candidates.json"
DIGEST = OUT / "digest.json"

# 每批翻譯幾則。改抓全文後單則輸入可達 6000 字，批次太大不利重試
# （一則不合格就整批重跑），也容易讓模型後面幾則草率帶過
BATCH_SIZE = 5
URL_IN_TEXT = re.compile(r"https?://|www\.", re.IGNORECASE)
# 繁簡錯誤的辨識標記：translate_batch 靠它把「只是字形不合」跟
# 「內容不可信」分開處理，不要改字面
SIMPLIFIED_TAG = "譯文出現簡體字"

# 摘要目標放在「讀完就懂，不必點進原文」。這個字數只有在 extract.py 抓到
# 原文全文時才撐得起來——沒有原文卻要求長摘要，等於逼模型編造。
#
# 攔截線比目標值寬鬆：實測兩者訂成一樣時，模型常寫到只超出幾個字的長度，
# 整批因此重跑，成本直接翻倍。攔截線是用來擋文字牆（實測有 797 字的），
# 不是用來計較那幾個字。
TITLE_ASK, TITLE_MAX = 45, 75
SUMMARY_ASK, SUMMARY_MAX = 400, 850

# ── 繁簡檢查 ────────────────────────────────────────────────
# 2026-08-29 實測：第 2 批五則全部回簡體，同一天其他四批完全正常。
# 提示詞早就寫著「台灣繁體中文」，但程式端只驗長度與 id，字形無人把關。
# 那批的用語其實是台灣的（「代理程式」「軟體」而非「程序」「软件」），
# 模型照著指示翻了，只是字形輸出成簡體——所以這是輸出品質的隨機失誤，
# 不是提示詞沒講清楚，也不是簡中來源的原文直接放行（五則裡四則來自英文來源）。
#
# 只列「簡體專用」的字：兩邊寫法相同的字（黑、鼓、霉、台、里…）不能列進來，
# 否則「黑客松」「擊鼓傳花」會被誤判。
SIMPLIFIED_ONLY = set(
    "这说时长问门车马见贝页风飞东专业丛丝严个丰临为举义乐习乡书买乱争云亚产亲亿仅从仓仪"
    "们价众优会传伤伦伪体佣侠侦侧侨债倾偿储儿兑兰关兴养兽冈军农冲决冻净减凤凭击刘则刚创"
    "剂剑剧劝办务动势勋区医华协单卖卢卫厂厅历厉压厌参双发变叠号叹员响哑哗唤团园围国图圆"
    "圣场坏坚坛坝坞坟坠垄垒垦执扩扫扬扰抚抛抠抢护报担拟拢拣拥拦拧拨择挂挚挟挠挡挣挤挥损"
    "捡换据掳掷掸摄摆摇撑敌敛数斋斗断无旧旷显晋晒晓晕晖暂术朴机杀杂权条来杨极构枢枣标栈"
    "栋栏树样档桥桦梦检楼欢欧歼残殴毁毕毡气氢汇汉汤沟沥沦沧沪泞泪泼泽洁洒浅浆浇浊测济浏"
    "浑浓涂涛涝涡涣涤润涧涨涩渊渐渔渗湾湿溃溅滚滞满滤滥滨滩潜灭灯灵灾炉点炼烂烛烟烦烧热"
    "爱爷牵状独狭狮猎猪献玑环现玛琐璃电画畅疗疟疮疯痪皱盏盐监盖盗盘睁瞒矫矿码砖础确碍礼"
    "祸离种积称稳穷窃窍窜窝窥竖竞笃笋笔笼筑筛签简篮类粪粮紧纠红约级纪纫纬纯纱纲纳纵纷纸"
    "纹纺线练组绅细织终绍经绑绒结绕绘给绚络绝绞统绣继绩续绯绳维绵综绿缀缄缅缆缉缓缔编缘"
    "缚缝缠缩缴罗罚罢羁翘耸耻聂聋职联聪肃肠肤肮肾肿胀胁胆胜胶脏脐脑脓脸腊腾舰艰艳艺节芜"
    "苇苏苹茎荐荡荣药莱莲获莹莺萝萤营萧萨蒋蓝蔼虏虑虫虽虾蚁蚂蚕蛮衅衔补衬袄袜袭装褛观规"
    "觅视览觉誉计订认讥讨让训议讯记讲讳许讹论讼讽设访诀证评诅识诈诉诊词译试诗诚话诞询该"
    "详诫误说诵请诸读课谁调谅谈谊谋谍谎谐谓谚谜谢谣谨谬谭谱谴贝贞负贡财责贤败账货质贩贪"
    "贫贬购贮贯贱贴贵贷贸费贺贼贾贿资赁赂赃赅赈赊赋赌赎赏赐赔赖赘赚赛赞赠赡赢赣赵赶趋跃"
    "践跷踊踪蹑躯车轨轩转轮软轰轴轻载轿较辅辆辈辉辐辑输辖辗辙辞辩边辽达迁过迈运还这进远"
    "违连迟适选逊递逻遗邓邮邹郑酝酱释鉴针钉钓钙钝钟钢钥钦钩钮钱钻铁铃铅铜铝铭银铸铺链销"
    "锁锅锋锐错锡锦键锯镇镜长门闪闭闯闲间闷闹闻阀阁阅队阳阴阵阶际陆陈险随隐难雏雾靓静韦"
    "韧韩页顶顷项顺须顽顾顿颁预领颈频颖题颜额风飞饥饭饮饰饱饲饶饼馆马驭驯驰驱驳驴驶驻驼"
    "驾骂验骑骗骚骤髅鱼鲁鲜鸟鸡鸣鸥鸦鸭鸿鹅鹏鹤鹰鹿麦黄齐齿龄龙龟"
)
CJK = re.compile(r"[一-鿿]")

# 門檻要留很寬的餘裕，因為批次連續三次驗證失敗會 sys.exit 中止整份日報——
# 為了幾個字殺掉一整天的日報不成比例。
# 22 天 553 則的實測分布：真正出問題的六則是 65～87 個簡體字（佔比 23～29%），
# 次高的只有 4 個（多為專有名詞，如「騰訊」「藍馳」寫成簡體），中間是巨大空隙。
# 訂在 8 個字落在空隙正中央；佔比條款是為了接住「整段簡體但篇幅很短」的情況。
SIMPLIFIED_MIN_CHARS = 8
SIMPLIFIED_MIN_RATIO = 0.15


def simplified_hits(text: str) -> tuple[int, float]:
    """回傳 (簡體專用字數, 佔全部中日韓漢字的比例)。"""
    if not text:
        return 0, 0.0
    cjk = len(CJK.findall(text))
    n = sum(1 for c in text if c in SIMPLIFIED_ONLY)
    return n, (n / cjk if cjk else 0.0)


def looks_simplified(text: str) -> tuple[bool, int]:
    n, ratio = simplified_hits(text)
    hit = n >= SIMPLIFIED_MIN_CHARS or (n >= 3 and ratio >= SIMPLIFIED_MIN_RATIO)
    return hit, n

# 送進模型的原文長度上限。extract.py 存的是 6000 字，但實測餵滿 6000 字時，
# 模型摘要會跟著失控（出現 939～1060 字的輸出而被退回重譯，成本多三成）。
# 新聞的關鍵事實幾乎都在前段，截到這裡對摘要品質影響很小，卻同時省下
# 輸入 token 與重試次數。長篇評論會被截斷，這是刻意的取捨。
INPUT_CHARS = 4000

GROUPS = ["模型與產品發布", "產業與資本", "研究與論文", "遊戲與娛樂", "政策與法規", "工程與工具", "其他"]
KINDS = ["譯", "摘", "僅標題"]

GLOSSARY = """inference→推論　fine-tune→微調　prompt→提示詞　token→詞元
open-weights→開放權重　benchmark→基準測試　agent→代理程式　agentic→代理式
inference cost→推論成本　context window→脈絡長度　multimodal→多模態
checkpoint→檢查點　distillation→蒸餾　quantization→量化　embedding→嵌入向量"""


# ────────────────────────────────────────────────────────────
# 第一階段：策展
# ────────────────────────────────────────────────────────────
CURATE_SYSTEM = f"""你是 AI 情報日報的策展編輯。輸入是一份已經由程式收集、去重、評分的候選清單。

你的工作只有三件事，全部只能輸出項目 id，不得輸出任何網址：

1. **合併重複**：同一則新聞的不同語言版本（例如英文原文與中日文轉載）標題不相似，
   程式的去重抓不到，必須由你判斷。合併時以資訊最完整的那則為主，其餘列為 duplicate。
2. **剔除無實質內容者**：純引言、單純的連結轉貼、與 AI 無關的項目、
   內容重複的週報彙整。理由要具體。
3. **分組與排序**：分到這些組別之一 {GROUPS}，並依重要性排序（rank 從 1 開始，不得重複）。
   「其他」是最後手段，只給真的無法歸類的項目——同一份日報裡「其他」超過三則
   就代表你分類太懶。評論、觀點文章歸到它談論的主題所屬組別。

排序依據**只能**是輸入提供的訊號：Hacker News 分數、報導家數、來源類型（官方公告優先）、
以及標題本身的資訊量。不要依據你對這些事件的既有知識判斷重要性——
你的訓練資料可能過時，而這份清單是今天的實況。

判斷「熱門」時請注意：Hacker News 分數高代表英文技術社群關注度高；
報導家數多代表跨媒體擴散。兩者都缺但來自官方公告的項目仍應保留。

使用者特別關注：**遊戲與娛樂產業的 AI 應用**、各大 AI 公司的最新消息、以及當下熱門議題。
遊戲相關項目若確實涉及 AI，請往前排。"""

CURATE_CONTRACT = """{
  "selected": [
    {"id": "候選清單裡的 id，原字串照抄",
     "group": "只能是這七個之一：模型與產品發布／產業與資本／研究與論文／遊戲與娛樂／政策與法規／工程與工具／其他",
     "rank": 1,
     "duplicate_ids": ["被合併進這則的其他 id，沒有就給空陣列 []"]}
  ],
  "dropped": [
    {"id": "被剔除的 id", "reason": "具體理由"}
  ]
}

最外層只能有 selected 與 dropped 兩個鍵，兩者都必須存在（沒有內容就給空陣列）。
selected 的每個元素只能有 id、group、rank、duplicate_ids 這四個欄位，
不得加入 title、reason、score 等任何其他欄位。rank 從 1 開始且不重複。"""


def _shape_curate(data: dict) -> None:
    if not isinstance(data.get("selected"), list):
        raise LLMError("缺少 selected 陣列")
    if not isinstance(data.get("dropped"), list):
        raise LLMError("缺少 dropped 陣列")
    for s in data["selected"]:
        if not isinstance(s, dict) or not isinstance(s.get("id"), str) or not s["id"]:
            raise LLMError("selected 項目缺少 id")
        if s.get("group") not in GROUPS:
            raise LLMError(f"group「{s.get('group')}」不在允許清單：{GROUPS}")
        if not isinstance(s.get("rank"), int):
            raise LLMError(f"{s['id']} 的 rank 不是整數")
        if not isinstance(s.get("duplicate_ids"), list):
            raise LLMError(f"{s['id']} 缺少 duplicate_ids 陣列")
    for d in data["dropped"]:
        if not isinstance(d, dict) or not isinstance(d.get("id"), str):
            raise LLMError("dropped 項目缺少 id")


def curate(pool: list[dict], target: int, model: str) -> dict:
    lines = []
    for i in pool:
        signals = []
        if i.get("hn_points"):
            signals.append(f"HN {i['hn_points']}分/{i.get('hn_comments', 0)}留言")
        # HF 投票一定要傳下去。2026-09-01 首次上線時漏了這個欄位，結果選進來的
        # 五篇論文在策展階段全被剔除，理由清一色是「無社群或媒體關注訊號」——
        # 它們正是因為有 HF 投票才被選進來的，只是策展編輯看不到。
        if i.get("hf_upvotes"):
            signals.append(f"HF Daily Papers {i['hf_upvotes']}票")
        if i["cross_source_count"] > 1:
            signals.append(f"{i['cross_source_count']}家報導")
        if i["pinned"]:
            signals.append("官方公告")
        summary = (i.get("summary_original") or "")[:200]
        lines.append(
            f"id={i['id']} | {i['source']}({i['lang']}/{i['cat']}) | "
            f"{'、'.join(signals) or '無額外訊號'}\n"
            f"  標題：{i['title_original']}\n"
            f"  摘要：{summary or '（無摘要）'}"
        )

    user = (
        f"候選項目共 {len(pool)} 則，請選出最多 {target} 則。\n\n"
        + "\n\n".join(lines)
    )
    return llm.ask_json(CURATE_SYSTEM, user, model, CURATE_CONTRACT, validate=_shape_curate)


# ────────────────────────────────────────────────────────────
# 第二階段：翻譯
# ────────────────────────────────────────────────────────────
TRANSLATE_SYSTEM = f"""你是專業的科技新聞譯者，把 AI 情報翻譯成**台灣繁體中文**。

**絕對規則（違反即為失敗）：**
1. 你只能改寫我提供的「標題」與「摘要」文字。**不得補充任何我沒有提供的資訊**——
   不得依據你的知識補背景、補後續發展、補數字、補人名職稱。
2. **不得輸出任何網址**。連結由程式另外附上，你的譯文裡不得出現 http、https 或 www。
   原文若提到網址或網域名稱，改寫成敘述（例如「該工具的網頁版」）或直接省略，
   不要照抄，更不要自己拼出一個網址。
3. 數字、金額、日期、模型版本號、產品名稱一律保留原文的寫法與精確度。
   原文說「reportedly（據報導）」就要譯出不確定性，不可寫成既成事實。
4. 若我提供的摘要是空的，`summary_zh` 就寫空字串，`kind` 標為「僅標題」。
   **絕對不要**根據標題想像內文。
5. 專有名詞、公司名、模型名保留英文原文（例如 GPT-5、Claude、Qwen、Gemini），
   不要音譯。
6. 我給幾則就回幾則，id 原封不動照抄，不得新增、不得遺漏、不得修改 id。
7. **全篇必須是繁體字。** 一個簡體字都不行——這→不是「这」，說→不是「说」，
   時→不是「时」，發→不是「发」，機→不是「机」，實測→不是「实测」。
   原文是簡體中文時特別容易照抄字形，務必逐字轉成繁體。
   專有名詞同樣要轉（騰訊、藍馳、華為），不要保留簡體寫法。
   程式會逐則檢查，含簡體字的整批會被退回重做。

**摘要的目標：讀者看完你的摘要就能掌握整則新聞，不必點進原文。**
我提供的多半是文章全文，請據此寫出完整的重點說明，包含：
發生了什麼事、牽涉到誰、關鍵數字與時間、為什麼重要、有沒有值得注意的但書或爭議。
不要只寫第一段，也不要寫成「本文介紹了……」這種空轉的目錄式描述。

**kind 的判定：**
- 「譯」：原文本身就短，你只是忠實翻譯
- 「摘」：原文是完整文章，你整理出重點（多數情況都是這個）
- 「僅標題」：沒有任何內文可用

**譯文風格與長度（硬性規定，超過會被程式退回重做）：**
- 標題：{TITLE_ASK} 字以內，簡潔，不加標點符號結尾
- 摘要：**{SUMMARY_ASK} 字以內、四到六句**。這是摘要不是翻譯，
  再長的文章都要壓進這個長度——挑最重要的事實，其餘捨棄。
  原文本來就只有一兩句時，照實寫短的，不要為了長度補充我沒給你的資訊。
- 週報、彙整型文章：寫「本期涵蓋哪幾件事」並點出其中最重要的兩三則，
  不要逐條翻譯。
- 用台灣讀者習慣的用語（軟體、程式、資料、網路，不用軟件、代碼、數據、網絡）
- 語氣中性，不加評論、不加感嘆

**術語對照表（請一致使用）：**
{GLOSSARY}"""

# 字數限制一定要寫在這裡。實測只寫在系統提示詞裡，模型會從 4000 字的原文
# 寫出 1500 字的摘要而被退回重譯；契約貼在使用者訊息末端才是模型最服從的位置。
TRANSLATE_CONTRACT = f"""{{
  "items": [
    {{"id": "輸入的 id，原字串照抄",
     "title_zh": "標題的繁體中文譯文，{TITLE_ASK} 字以內",
     "summary_zh": "摘要的繁體中文譯文，**{SUMMARY_ASK} 字以內、四到六句**；原文無內文時給空字串 \\"\\"",
     "kind": "只能是「譯」「摘」「僅標題」三者之一"}}
  ]
}}

items 的長度必須等於我給你的則數。每個元素只能有 id、title_zh、summary_zh、kind
這四個欄位，不得加入 url、source、date 等任何其他欄位。

**送出前逐則數一次 summary_zh 的字數。** 這是摘要不是全文翻譯：
原文再長都要壓在 {SUMMARY_ASK} 字左右，只留最重要的事實。
超過 {SUMMARY_MAX} 字會被程式退回，整批重做。"""


def _shape_translate(data: dict) -> None:
    if not isinstance(data.get("items"), list) or not data["items"]:
        raise LLMError("缺少 items 陣列或陣列為空")
    for t in data["items"]:
        if not isinstance(t, dict):
            raise LLMError("items 元素不是物件")
        for key in ("id", "title_zh", "summary_zh", "kind"):
            if not isinstance(t.get(key), str):
                raise LLMError(f"項目缺少字串欄位 {key}：{str(t)[:120]}")
        if t["kind"] not in KINDS:
            raise LLMError(f"kind「{t['kind']}」不在允許清單：{KINDS}")


def translate_batch(batch: list[dict], model: str) -> dict:
    lines = []
    for i in batch:
        original = (i.get("summary_original") or "")[:INPUT_CHARS]
        # 標明手上這份是全文還是 feed 導言：模型才知道能不能寫出完整摘要，
        # 還是只有一段導言、必須克制
        if i.get("text_source") == "article":
            label = "原文全文"
            hint = "（有完整內文，請寫出讀完就能掌握全貌的摘要）"
        else:
            label = "原文摘要"
            hint = "（只有 feed 提供的片段，寫得到多少算多少，不要補充額外資訊）"
        lines.append(
            f"id={i['id']}（來源語言：{i['lang']}，來源：{i['source']}）{hint}\n"
            f"標題：{i['title_original']}\n"
            f"{label}：{original or '（無內文可用，請標為僅標題）'}"
        )
    user = f"請翻譯以下 {len(batch)} 則：\n\n" + "\n\n---\n\n".join(lines)

    def validate(data: dict) -> None:
        _shape_translate(data)
        # 語意檢查也放進重試迴圈——否則模型重跑時不知道自己哪裡錯了，
        # 實測會一模一樣地再錯一次
        errors = verify(data["items"], batch)
        if errors:
            raise LLMError("；".join(errors))

    try:
        return llm.ask_json(TRANSLATE_SYSTEM, user, model, TRANSLATE_CONTRACT, validate=validate)
    except LLMError as e:
        # 繁簡是字形問題，內容本身可信——不值得為它中止整份日報。
        # 三次都沒改過來時，只要「剩下的錯誤全是繁簡」就放行並記警告；
        # 只要還混著腦補、假網址、漏譯這類真正不可信的錯誤，一律照舊中止。
        partial = getattr(e, "partial", None)
        if not partial:
            raise
        try:
            _shape_translate(partial["data"])
        except LLMError:
            raise e from None
        rest = [x for x in verify(partial["data"]["items"], batch) if SIMPLIFIED_TAG not in x]
        if rest:
            raise
        print(f"  警告：繁簡檢查重試 {partial['attempts']} 次仍未通過，"
              f"接受此輸出（內容驗證均通過，僅字形不合）")
        partial["degraded"] = "simplified"
        return partial


# ────────────────────────────────────────────────────────────
# 驗證：模型輸出必須對得上輸入
# ────────────────────────────────────────────────────────────
def verify(translated: list[dict], batch: list[dict]) -> list[str]:
    errors = []
    want = {i["id"] for i in batch}
    got = {t["id"] for t in translated}

    for extra in got - want:
        errors.append(f"輸出了輸入中不存在的 id：{extra}（疑似捏造）")
    for missing in want - got:
        errors.append(f"漏譯：{missing}")

    by_id = {i["id"]: i for i in batch}
    for t in translated:
        src = by_id.get(t["id"])
        if not src:
            continue
        if URL_IN_TEXT.search(t["title_zh"] + t["summary_zh"]):
            errors.append(f"{t['id']}：譯文含網址（模型不應生成連結）")
        original = (src.get("summary_original") or "").strip()
        if not original:
            if t["kind"] != "僅標題" or t["summary_zh"].strip():
                errors.append(f"{t['id']}：原文無摘要，卻產出了摘要內容（疑似腦補）")
        elif len(original) >= 600 and not t["summary_zh"].strip():
            # 反向的失敗：有內文卻交白卷。這樣的項目在頁面上只剩一個標題加連結，
            # 讀者等於什麼也沒得到，而且會被標成「原始來源未提供摘要」——那是假的
            errors.append(
                f"{t['id']}：原文有 {len(original)} 字內文，你卻沒有產出摘要。"
                f"請確實寫出摘要"
            )
        if not t["title_zh"].strip():
            errors.append(f"{t['id']}：標題譯文為空")
        if len(t["title_zh"]) > TITLE_MAX:
            errors.append(f"{t['id']}：標題 {len(t['title_zh'])} 字，超過 {TITLE_MAX} 字上限，請精簡")
        if len(t["summary_zh"]) > SUMMARY_MAX:
            errors.append(
                f"{t['id']}：摘要 {len(t['summary_zh'])} 字，超過 {SUMMARY_MAX} 字上限。"
                f"你把整篇文章翻完了，不是在摘要。請只保留最重要的事實，"
                f"壓到 {SUMMARY_ASK} 字左右"
            )
        bad, n = looks_simplified(t["title_zh"] + t["summary_zh"])
        if bad:
            errors.append(
                f"{t['id']}：{SIMPLIFIED_TAG}——譯文含 {n} 個簡體字。"
                f"請整則改用台灣繁體中文重寫（例如 这→這、说→說、时→時、"
                f"发→發、机→機、软体→軟體、实测→實測）。用語已經正確，只要改字形"
            )
    return errors


# ────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只處理前 N 則（試跑用）")
    ap.add_argument("--target", type=int, default=30, help="策展後保留幾則")
    ap.add_argument("--skip-curate", action="store_true", help="跳過策展，直接翻譯（除錯）")
    ap.add_argument("--curate-model", default=llm.MODEL_CURATE, help="策展用模型（需要判斷力）")
    ap.add_argument("--translate-model", default=llm.MODEL_TRANSLATE, help="翻譯用模型")
    args = ap.parse_args()

    if not llm.available():
        sys.exit("找不到 claude CLI，請先安裝並登入（npm i -g @anthropic-ai/claude-code）")
    if not CANDIDATES.exists():
        sys.exit(f"找不到 {CANDIDATES}，請先執行 python collect.py")

    data = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    # papers 一定要一起讀進來。它從專案一開始就被 collect.py 寫進 candidates.json，
    # 卻沒有任何地方讀取——arXiv 抓了 13 天、一篇都沒上過稿。
    # collect.py 已經改成「只有被 HN 討論或 HF Daily Papers 收錄的論文才會進這個
    # 清單」，所以這裡放行的都是有外部訊號的，不是隨機五篇。
    # 對應的訊號要在 curate() 的 signals 裡標出來，否則策展編輯會當成沒訊號而剔除。
    pool = data["official"] + data["ranked"] + data.get("papers", [])
    by_id = {i["id"]: i for i in data["all_scored"]}
    if args.limit:
        pool = pool[: args.limit]

    cost_total = 0.0
    usage_total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

    def account(res):
        nonlocal cost_total
        cost_total += res["cost"]
        for k in usage_total:
            usage_total[k] += res["usage"][k]

    # ── 第一階段：策展 ──
    selected, dropped = [], []
    if args.skip_curate:
        selected = [{"id": i["id"], "group": "其他", "rank": n, "duplicate_ids": []}
                    for n, i in enumerate(pool, 1)]
    else:
        print(f"策展中（{args.curate_model}）：{len(pool)} 則候選 → 最多 {args.target} 則…")
        try:
            res = curate(pool, args.target, args.curate_model)
        except LLMError as e:
            sys.exit(f"策展失敗：{e}")
        account(res)
        valid_ids = {i["id"] for i in pool}
        seen = set()
        for s in res["data"]["selected"]:
            if s["id"] not in valid_ids:
                print(f"  警告：策展輸出了不存在的 id {s['id']}，已捨棄")
            elif s["id"] in seen:
                print(f"  警告：策展重複輸出 id {s['id']}，已捨棄")
            else:
                seen.add(s["id"])
                selected.append(s)
        dropped = [d for d in res["data"]["dropped"] if d.get("id") in valid_ids]
        selected.sort(key=lambda s: s["rank"])
        selected = selected[: args.target]
        print(f"  選入 {len(selected)} 則，剔除 {len(dropped)} 則")
        merged = sum(len(s["duplicate_ids"]) for s in selected)
        if merged:
            print(f"  合併了 {merged} 則跨語言／跨來源重複")

    # ── 第二階段：翻譯 ──
    items = [by_id[s["id"]] for s in selected if s["id"] in by_id]
    results: dict[str, dict] = {}

    degraded_batches = []
    for start in range(0, len(items), BATCH_SIZE):
        batch = items[start : start + BATCH_SIZE]
        print(f"翻譯 {start + 1}-{start + len(batch)}／{len(items)}（{args.translate_model}）…")
        try:
            # ask_json 內含結構＋語意驗證與重試，通過才會回來
            res = translate_batch(batch, args.translate_model)
        except LLMError as e:
            sys.exit(f"翻譯驗證連續失敗，中止以免產出不可信內容：{e}")
        if res.get("degraded"):
            # 降級一定要留下紀錄。靜默放行等於下次沒人知道發生過，
            # 而繁簡問題正是這樣拖了十天才被使用者發現的。
            degraded_batches.append({
                "batch": f"{start + 1}-{start + len(batch)}",
                "reason": res["degraded"],
                "ids": [i["id"] for i in batch],
            })
        account(res)
        for t in res["data"]["items"]:
            results[t["id"]] = t

    # ── 組合輸出：網址與時間由程式 join，模型碰不到 ──
    digest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_window_hours": data["window_hours"],
        "model": {"curate": args.curate_model, "translate": args.translate_model},
        "items": [],
        "dropped": dropped,
        "usage": usage_total,
        "cost_usd": round(cost_total, 4),
        "degraded_batches": degraded_batches,
    }
    for s in selected:
        src = by_id.get(s["id"])
        tr = results.get(s["id"])
        if not src or not tr:
            continue
        digest["items"].append({
            "id": s["id"],
            "group": s["group"],
            "rank": s["rank"],
            "title_zh": tr["title_zh"],
            "summary_zh": tr["summary_zh"],
            "kind": tr["kind"],
            # 以下全部來自 candidates.json，模型無法影響
            "title_original": src["title_original"],
            "url": src["url_raw"],
            "source": src["source"],
            "lang": src["lang"],
            "published_utc": src["published_utc"],
            "hn_points": src.get("hn_points", 0),
            "hn_url": src.get("hn_url", ""),
            "cross_source_count": src["cross_source_count"],
            "also_reported_by": src.get("also_reported_by", []),
            "merged_ids": s["duplicate_ids"],
            "time_clamped": src["time_clamped"],
            "social": bool(src.get("social")),
        })

    OUT.mkdir(exist_ok=True)
    DIGEST.write_text(json.dumps(digest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n完成 {len(digest['items'])} 則 → {DIGEST}")
    print(
        (f"⚠ 有 {len(degraded_batches)} 批降級放行："
         + "、".join(d["batch"] + f"（{d['reason']}）" for d in degraded_batches)
         + "\n" if degraded_batches else "")
        + f"Token：輸入 {usage_total['input']:,}、輸出 {usage_total['output']:,}"
        f"（快取寫入 {usage_total['cache_write']:,}／讀取 {usage_total['cache_read']:,}）"
        f"　CLI 回報成本 US${cost_total:.4f}"
    )

    print("\n── 譯文預覽（前 5 則）──")
    for i in digest["items"][:5]:
        print(f"\n[{i['group']}] {i['title_zh']}")
        print(f"  原題：{i['title_original'][:60]}")
        print(f"  {i['kind']}：{i['summary_zh'][:100] or '（無摘要）'}")
        print(f"  {i['source']} · {i['url'][:80]}")


if __name__ == "__main__":
    main()
