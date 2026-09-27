# 写手 — AI 怎么在平台上写一颗镜头的 prompt

> 出处标签：**【HF N/12】** Higgsfield 12 部官方片里 N 部这么做（2026-09-27 起算上第 12 部 Passport Rush；计数见 `production/HF_CANONICAL.md` §6、§11、§13；计数脚本基于未分发的私人镜像，脚本不随库提供），7 部及以上（过半）是多数；**【HF 参考 N/12】** 不到 7 部（6 部是一半，不算多数），只作参考。6 部社区片是别人用 HF 工具拍的，不算进部数（老板 2026-09-27：官方 12 部，社区单列）；**【HF 手册】** 只写在 HF 的 skill 手册里（`cinedance:行` / `acting-system:行` / `lira:行` 指 `skills/`），没法按部数数；**【HF 例】** 一部简报里的一行原话（`片名:行` 指 `briefs-clean/`），没有数过几部这么做；**【老板】** 老板的决定；**【本库】** 我们自己的做法或推论，可能错。
>
> **给谁看**：任何一个要当写手的 AI session。读完这一页，不用问人，就该能写。
>
> **一句话**：老板提想法，写手写**整条** prompt，写进卡上的 `_production.prompt`；写完交给一个新开的审查员 AI 照手册查，每条意见都改、只改出问题的那一段；然后下拍摄单。平台原样发出你的文字，只在你没写的时候补五样东西（第四节）；没有一条手艺规则会拦下一条 take。

## 一、这是照谁做的

**【HF 例】** `hell-grind:61`：「We wrote prompts together with Claude」——人和 AI **一起**写。它把整个项目文件夹装进上下文：剧本、角色表、注册表、带 @tag 的镜头清单。规矩打包成 skill，「Claude loads by itself and then works by」（`cully:22`）。

**【HF 例】** `cully:25`：CINEDANCE 里是三个部件——**写手**「builds the whole prompt」，把一场戏拆开、定站位光学物理时间线；**审计员**「re-checks every prompt before it goes out」；**工作台**「patches only the section that failed, because a fully rewritten prompt loses the parts that already worked」。

**【老板 2026-09-26】** 平台上的对应关系：

- **写手** = 你（这个 session）。整条 prompt 都是你写的，平台不替你写任何一段。
- **审查员** = 另开一个**新的** AI（没写过这些卡），领同一版手册，照手册查你写好的 prompt。它的每条意见都要写明违反了手册哪一行；指不出手册哪一行的，不算意见。
- **工作台** = 在文件夹里改那颗镜头的 `shotlist.md` 段落，再 `mvgp shoot`：每条意见都要改（`cully:25` 没有"可以不改"的选项），只改出问题的那一段，其余逐字不动；每条意见回一行"改了哪一段"。意见和你的回答一起附在拍摄单上，看片台上看得到。

**【老板 2026-09-26】** 平台不审你的活，也不因为没审就拦：没附审查意见的镜头，看片台上标「没审」。检查通过不等于画面成立，实际看片和老板的选择才算数。

## 二、开工先装什么

按 `hell-grind:61` 的做法，把这些读进上下文再动笔：

| 装什么 | 怎么拿 |
|---|---|
| 手册 | `mvgp open <片子文件夹>`：把本页（`writer.md`）、CINEDANCE、ACTING、LIRA 写到 `<films>/.manuals/<版本>/`；平台记下你的凭证领了哪一版（`mvgp` = `.venv-production/bin/python -m production.cli`，见 `production/AGENT_GUIDE.md`） |
| 项目与老板的话 | `mvgp pull <片子文件夹>` 写出 `log.md`：每颗镜头的版本、这版改了什么、老板的判定和理由、他按条按秒写的备注；选中的片下到 `TAKES/` |
| 场与资产 | 你文件夹里的 `registry.md`（每个资产的描述 `descriptor`、每个世界的画风 look）和 `ASSETS/<类>/<tag>.md`（出图提示词，图在旁边） |
| 镜头与已有的 prompt | `SCENE NN - 名字/shotlist.md` 里那颗镜头的段落；已有整条 prompt 时，你只改出问题的那一段，其余逐字不动 |

**【老板 2026-09-26】** 没领当前版本的手册、卡上的 `_production.playbook_version` 不是这一版，平台都不拦，只在看片台上标「手册不是最新」。照样要领最新的：手册是你写对的依据。

## 三、老板提想法，你写

老板说的是**这场戏想怎么拍**：谁在哪、谁先动、镜头怎么看、光从哪来。你的活是把它写成一整条 prompt——不是翻译，是**按手册拆解**（`cully:25`「breaks the scene down and sets the blocking, the optics, the physics and the timing」）。

**【本库】** 在老板已经授权的方向内，根据项目意图与适用规范完成具体拍法。原创依据剧本与导演理解；复刻依据核实的画面与声音（阅读开关开着时，先 `observe` 原片，每颗镜头的 `source-understanding` 引用那次观察），不补写未被支持的事件和动机。复刻前期见 [复刻拍片规范](recreation.md)；看片方法见 [看片复盘规范](screening-review.md)。

## 四、写整条 prompt：卡上的 `_production.prompt`

**【老板 2026-09-26】** 每一段都是你写的。默认用 CINEDANCE 的条目：SCENE CONTEXT · ACTIVE REFERENCES · LOCATION MAP · FIRST FRAME AND SPATIAL BLOCKING · FORMAT MODE · OPTICS · CAMERA · ACTION TIMING · PHYSICS · LIGHTING · AUDIO · CHARACTER ACTING · STYLE · QUALITY · POSITIVE CONSTRAINTS（顺序照 `cully:85`）。**【HF 参考 3/12】** 这套条目骨架在 12 部里只有 3 部当多数做法用；**【HF 手册】** `cinedance:157-176`「Do not treat every section as mandatory」，用不上的段就不写。

**资产描述由你贴。【HF 7/12】** 资产 = 一段文字描述 + 一张图，描述进每条 prompt（`adiliada:34`、`cully:44`、`hell-grind:21`、`if-you-stop:56`、`oneiric:26`、`red-flag:14`、`trigger(BD):25`）。**【HF 例】** 描述可以按这颗镜头改写：oneiric 的一条 prompt 把 `@char_ON_Sam_s2_v1` 写成「slim 18-year-old man … a floppy slice of pizza in his hand; deep inside the story he is telling」，比注册表多了这一镜的状态。一个状态是一个单独的资产、单独的名字（**【HF 参考 6/12】** 6 部简报写了：`cully:55`、`hell-grind:48`、`oneiric:35` 等；Passport Rush 的简报没写，但它的元素也这么分，如 `taxi_noroof`），比如 `@kel_wet`。平台一个资产只放一张图，所以状态仍然单独起名。

**视频参考（走 Higgsfield 时）**：**【HF 例】** Passport Rush 不用角色设定图，用 360 度转身视频（带表情和声音）当角色参考（`passport-rush:24`），复杂动作先在 Blender 做灰模预演，再当动作、机位参考（`passport-rush:54-57`）；它 70% 的 Seedance 2.5 镜头带视频参考。**【HF 参考 1/12】** 12 部官方片里只有这一部当多数做法用。做法：把视频放在 `ASSETS/<类>/<标签>.mp4`（代替 `.png`），平台照 HF 的写法把它编成 `<<<video_1>>>`，图片编成 `<<<image_1>>>`，两套分开编号（Passport Rush 带视频参考的 6,750 条里 6,452 条这么写；fal 的 `@Image1` 只在样片模式下用）；在 prompt 里写清它管什么，比如 `@previs — motion, camera and timing reference ONLY; the grey-box look is not inherited`（Passport Rush 原话的意思）。样片模式（fal）不收视频参考。

**表演**：**【HF 手册】** 每人一段流水文，以这个人的 @tag 开头，不要标题、不要列表和字段（`acting-system:236-259`）；写行为不写情绪（**【HF 参考 5/12】** `cully:109-113`、`hell-grind:89-95`）。

**平台只补这五样，而且只在你没写的时候补（【老板 2026-09-26】；`production/prompt.py`）：**

| 补什么 | 什么时候补 | 补在哪 |
|---|---|---|
| 参考编号（Higgsfield 上照 HF 写 `<<<image_1>>>`、视频 `<<<video_1>>>`；样片模式的 fal 上是 `@Image1`） | 你写的每个 @tag，只要它有选中的资产图或视频 | 紧贴在你的 @tag 前面。编号按 @tag 第一次出现的顺序；默认只补在第一次出现处（老板 2026-09-27 按实测定：每次都补时模型把参考图各演成单人镜头），项目可改成每次都补。你自己不要写 `@Image1` |
| 资产描述 | 你哪一行都没描述这个 @tag（没有一行同时有这个 @tag 和至少 4 个别的词） | 贴注册表原文：你有 `@kel (CAL):` 这种只有 tag 的参考行，就接在它后面；否则加在你的参考段末尾（ACTIVE REFERENCES / REFERENCES / CHARACTERS / SUBJECT LOCK）；没有参考段就在开头加一段 ACTIVE REFERENCES |
| 画风 | 你没写 STYLE、LOOK、GRADE、COLOUR / COLOR（含 COLOUR / LIGHT）段，也没有 `Style:` 开头的行 | 贴这颗镜头的画风原文（卡上 `_production.look` 写画风资产的 tag；只有一个画风时默认它），作为 STYLE 段放在 QUALITY / POSITIVE 段前面，没有就放末尾 |
| 时长 `N s.` | 全文没写任何秒数 | 末尾。写了但和卡上秒数不同，只提醒 |
| `No music.` | 全文没提 music、score、song、音乐、配乐 | 末尾 |

平台不加别的一个字。看片台打开一条 take，能看到发出去的全文，平台补的地方都标出来，下面写着「发出去的 = 写手原文 + N 处补充 ✓」。

**【HF 例】** 为什么描述和画风是固定文字：`cully:133`「Descriptors and the fixed look-and-camera block of each world live as constants」。**【HF 参考 5/12】** 风格前缀逐字贴（7 部简报写了，5 部的多数 prompt 这么做；Passport Rush 简报建议的前缀在它自己的 prompt 里一次也没原样出现，`passport-rush:117-118`）。

## 五、写之前：从手册里取，不自编

**语言库**（`cinedance-v4-seedance.md`）——句子不是你想的，是选的：

| 要写什么 | 翻哪里 |
|---|---|
| OPTICS 的视场角与镜头性格 | `cully:91-94` 十档视场角由内容选择；`cinedance:643-712` 提供六种展开例句。保留适用的光学含义，同一颗内锁定选择；不是每颗照抄整段 |
| 动词 | `cinedance:1219-1261`「Seedance-safe language」——stands / faces / looks / holds / walks…用这些，别的少用 |
| 首帧站位怎么写 | `cinedance:329-355`（首帧占位）· `:355-403`（空间站位）· `:403-427`（视线） |
| 地标距离 | `cully:101` 可见地标；`hell-grind:82` 米数。**【本库】** 优先可见关系，必要时补数量。raw Cully `cully-raw:265-267` 的三种尺度前丢了主语，不能把它当已证实的通用距离命令 |
| 相机 | `cinedance:787-850`——动或不动、跟不跟；手持规则 `:830` |
| 物理 | `cinedance:850-930` |
| 光 | `cinedance:930-974` |
| 时间线 | `cinedance:974-1002` 时间块；`if-you-stop:110-113` 区分 2.0 序数与 2.5 锁机长镜头阶段。结束状态属于本镜头，不提前演下一镜。**【HF 7/12】** 一条里可以切镜头（老板 2026-09-25 同意），每一刀写进 ACTION TIMING（hell-grind 正好卡在一半上，数法不同时是 6/12） |
| 表演 | `acting-system.md`——写行为不写情绪；眼睛要活（`:215`）；优先明确可见动作状态（`:281-287`），必要的接触、位移和反应不能删掉；每人一段流水文，以 @tag 开头（`:236-259`） |

**多数做法（默认照做）**：**【HF 9/12】** 英文 · **【HF 11/12】** 长 prompt · **【HF 10/12】** 写明「no music」· **【HF 9/12】** 写明焦段（mm 或视场角）· **【HF 8/12】** 写明时长（2026-09-27 重数，数法见 `production/HF_CANONICAL.md` §11；`if-you-stop:128`「the model improvises the extra seconds」）· **【HF 12/12】** 参考按 @tag 或按图的位置叫（按图的位置叫是 7/12，只按 @tag 叫是 6/12）。平台对缺焦段、缺拍子只提醒；没写时长，平台在末尾补 `N s.`。

**参考做法（可用，平台不查）**：**【HF 参考 6/12】** 按秒写动作拍子（如 `0-4s | …`；11 部时 6/11 算多数，加上 Passport Rush 正好一半）· **【HF 参考 3/12】** 十五条目骨架 · **【HF 参考 5/12 简报】** 首帧已经站满人（`cully:31`、`cinedance:336`）。

## 六、写完：静默自查 20 问

**【HF 手册】** `cinedance:1272-1297`「Silent self-QA before output」——写完先自己答一遍（20 问在 `:1276-1295`），「If any answer is no, fix the prompt before output」。答案不进 prompt：`cinedance:149`「Do not output checklist.」

其中跟你写的直接相关的：首帧对不对、@tag 有没有过期、地理够不够清楚、镜头性格是否按内容选、光会不会变平、时间块是否一致、台词是不是只有剧本那句、有没有泄露上一镜的上下文、prompt 是不是英文。

## 七、审查员：新开一个 AI 照手册查

**【老板 2026-09-26】**（照 `cully:25` 的审计员）：

1. 自查完，把这场戏的剧本和写好的卡交给一个**新开的** AI 审查员（它没写过这些卡）。它读同一版手册（`<films>/.manuals/<版本>/`）。
2. 它只照手册查：每条意见写明违反手册哪一行（如 `cinedance:336`）。指不出手册哪一行的，不算意见，你不用改。
3. 每条意见你都要改，只改出问题的那一段（第十节），其余逐字不动。你对手册某一行的理解和审查员不同时，照审查员的改。
4. 每条意见回一行：改了哪一段。
5. 审查员的意见和你的回答一起附在拍摄单上，看片台上看得到；没附的镜头标「没审」。老板不用参与这一步。

## 八、交卡：写进 `shotlist.md`

每颗镜头一段（格式见 `production/FOLDER.md`）：

- 段头 `## <编号> · <秒>s · <一句话目标>`：秒数是整数（Higgsfield 和 fal 都收 4–30），目标用大白话中文，老板在看片台上读的就是它。
- `look: <世界>`（可选）：这颗镜头用哪个画风；项目只有一个画风时不用写。复刻的镜头再加 `source: <原片理解编号>`。
- 下面是你写的整条 prompt，原样发出。
- 手册版本、这一版改了什么、路线（默认 Higgsfield 1080p；开了样片模式是 fal 480p 草稿；16:9）都由平台记：推送时自动写"文件夹里改了哪些"。

然后 `mvgp quote` 看价钱，`mvgp shoot <文件夹> <镜头> --review <审查意见.json>` 下单；平台自己准备、每版发四条（默认 Higgsfield 1080p，样片模式下 fal 480p）、放上看片台。

## 九、交出去之后：拦的和提醒的

`mvgp shoot` 的回复里每颗镜头有 `stage`；被拦的镜头 `stage` 是 `stopped`，`reason` 说为什么。平台的检查分两类：

| 类 | 包括 | 该做什么 |
|---|---|---|
| `deny`（拦） | 花钱：预算 · 供应商限制：路线、画幅分辨率、4–30 整秒、最多 30 张参考图、prompt 最多 65,536 字节 · 发送完整性：发出去的不等于你的原文加允许的补充、候选被改过、准备之后卡又改了、画面锁 · 老板打开的开关：压测、复刻先看原片 | 照报错改，不绕 |
| `advisory` / `warning`（提醒） | 其余一切：手册不是最新、缺焦段或拍子、写的秒数和卡不同、@tag 不是这颗镜头选中的资产、注册表描述和你的描述在状态词上矛盾、条目顺序 | 当自查读，要紧的改；**从不拦 take** |

**【本库】** 准备之后卡又改了，旧候选作废，要重新准备。你要检查实际发出的文字和参考，再看生成的声画是否实现这段表演与整场意图。检查通过不等于看片通过。

## 十、改的时候：只改一段

**【HF 参考 2/12】** `hell-grind:106`：「Every iteration was surgical: one line changes, everything else stays word for word」（`cully:134` 同）。**【HF 例】** `cully:25`：工作台「patches only the section that failed」。平台不数你改了几行。

老板看完 take 说“第三条不对，他转头了”——先定位具体失效的句子，只改必要内容。在 `shotlist.md` 里改那一段，其余逐字不动，再 `mvgp shoot`；推送时平台把改了哪些记进版本日志「what changed」那一栏。整篇重写会把本来对的段写坏（`cully:25`）。

`mvgp pull` 写出的 `log.md` 和项目页按镜头列出版本日志：版本 / 这版改了什么 / 老板的判定。**【HF 参考 2/12】** 到第 10 版会提示「10–15 版还不成，就简化这个镜头」（`hell-grind:106`、`if-you-stop:206`）。只是提示，不拦。

## 十一、老板判废的时候：读他那句话

老板在看片台上选一条，或者回「再拍一批 / 都不行」加一句话；`mvgp pull` 把那句话和看片台上的备注（按条、按秒）写进 `log.md`。那句话就是这一版的失效点：定位到段，在 `shotlist.md` 里改那一段，再 `mvgp shoot`。他回「再拍一批」却没写理由时，平台自己把同一张卡原样再拍四条，你不用动。

**【HF 4 部实测】** 同一条 prompt 原样再拍很常见：trigger 42%、red-flag 48%、oneiric 52%、passport-rush 68% 的批次和之前某一批一字不差（2026-09-26 审计；passport-rush 2026-09-27 重数）。**【HF 参考 2/12】** 版本日志「version, what changed, verdict」（`cully:134`，137 条；`hell-grind:106`），平台的项目页替你记；判定是老板的选择，不是你的自评。

## 十二、不要做的事

- **不要**在动作段里写台词（**【HF 例】** `hell-grind:101`）。
- **不要**用视觉指令回指上一镜（previously / same as before / from last shot / as above / the other character——**【HF 手册】** `cinedance:1047-1065`）。AUDIO 内已声明的前文声音只作上下文，不重新说、也不借此引入视觉内容（`cinedance:1035-1043`）。
- **不要**默认写一整段 NEGATIVE CONSTRAINTS：**【HF 手册】** `cinedance:1176`「Do not output a standalone NEGATIVE CONSTRAINTS block by default」；手册留了明确请求和已知失败的例外（`cinedance:1174-1217`）。
- **不要**把时间轴写超过卡上的秒数（`if-you-stop:128`）；平台只收 4–30 整秒。
- **不要**自己写 `<<<image_1>>>`、`@Image1` 这类编号：平台按你的 @tag 编号，按路线用对的写法。
- **不要**把自查答案、清单或推理写进 prompt（`cinedance:149`）。
- **不要**把参考做法（十五条目骨架、四组卡、一行补丁、首帧站满人、按秒写台词）当门槛；可以用，不必要求。
- **不要**把自己的推测记成老板要求或 HF 原话。

## 十三、台词和时长（按秒写台词是参考做法）

**【HF 参考】** 按秒给每句台词标起止是少数：最早 11 部里有 10 部，这样写的 prompt 都不到 8%（2026-09-26 审计；Passport Rush 没重数）。例子：`oneiric:67`：「RUDY (voice off-screen) 1.0s–3.6s: "AI builds the whole thing around you in real time."SAM 3.8s–5.3s: "Bro, that's complete garbage."」、「Each character says only their own line, verbatim; when a character is not speaking, their mouth stays closed.」

**【HF 例】** `hell-grind:101`：一句台词的写法「The voice and its emotion → the line in quotes → the physical action → the facial reaction.」、「everyone speaks ONLY the line in quotes; whoever has no line stays completely silent」。要不要写这样的静音锁由你定；平台不替你加。

**【HF 例】** 时长在界面里先定：`if-you-stop:128`「Duration is declared in the interface before generation.」平台把卡上的秒数作为接口参数发出去；**【HF 8/12】** 多数项目也把时长写进 prompt。**【HF 例】** 一段跑不稳就切成两半，不加秒数：`if-you-stop:173`「If the action goes wrong in two takes out of three, the prompt is not rewritten — the run is split in half. Adding seconds is pointless」。

**【本库 / 少数】** 按秒写台词时的留白（2026-09-24 量，只来自 `data/gens-full.jsonl` 里 DIALOGUE 段给每句标了起止秒的 51 条 prompt）：第一句开口中位 1.3 秒，句与句之间中位 0.5 秒（四分之一在 0.4 秒以内），最后一句说完到镜头结束中位 4.0 秒（四分之一在 2.5 秒以内）。句子之间不要重叠（**【本库】**）。

**【老板 2026-09-26】** 一场戏分几个镜头：

1. 一场戏默认一个镜头拍完，最长到模型的上限：Seedance 2.5 是 30 秒，Seedance 2.0 是 15 秒。（**【HF 例】** HF 自己的做法不同：`cully:69`「One clip holds one speaker and one short line」，`cully:123` 一条约 12 秒「the shot length the model handles reliably」。）
2. 需要切镜头，就在同一次生成里切，这样不会接不上（第五节「时间线」一行）。
3. 要单独生成一个新镜头，必须写清楚它的新目的；写不清楚，就放回同一次生成里。

**【HF 例】** 2.5 用来拍长镜头：`if-you-stop:21`「used selectively, for complex micro-movements and long takes: it holds them far more reliably」。**【HF 参考 1/12】** 每颗镜头写一句话目的（Cully 的四组卡）：`cully:38`「Direction: the goal of the shot in one line」、`cully:40`「the holes show up before you spend a generation: a shot with no goal」。

**【本库】** 中文语速 HF 没有数据，用我们自己的片子量：2026-09-24 用 Whisper（mlx-community/whisper-large-v3-turbo）听两部片送审的 23 条 take，剔掉一条听错的，22 条共 59 段话，中位 3.85 characters per second（每秒 3.85 个汉字），慢的十分之一约 3.1。按秒写台词时，每句的起止秒数按**每秒 3 个字**算必须装得下（偏慢，给演员留气口）；装不下就删字、拆句或把这句挪到下一颗。
