# Proofline (v1.5)

> **Xưởng phần mềm nơi mọi thay đổi đều có bằng chứng.**  
> Một đội AI agent làm việc song song (kể cả xuyên đêm), mỗi thay đổi đi kèm chứng minh ở ba lớp: code, người dùng, production; quyền tự chủ được kiếm dần bằng số liệu.  
> Kết hợp mô hình thực chiến **Lauren Tan (30+ Autonomous Agents / `pstack`)** và nền tảng lý thuyết **Google Research Harness² (Recursive Agent Harnessing)**.

---

## 1. Giới thiệu tổng quan

**Proofline** là lớp **điều phối, kiểm chứng và tự chủ** bao quanh các AI coding agent có sẵn (Claude Code headless / Claude Agent SDK). Hệ thống tiếp nhận việc, phân phối cho nhiều worker chạy song song trong sandbox cách ly, tự kiểm chứng kết quả **như một người dùng thật**, ra quyết định theo **gu của Tech Lead**, và được **tự quyết nhiều hơn khi đã chứng minh được bằng số liệu**.

Điểm khác biệt không nằm ở việc "agent viết code", mà ở: **ưu tiên kiểm chứng, tự host cho code nội bộ, gu và playbook riêng của tổ chức được cải tiến có đo lường, và quyền tự chủ được kiếm dần chứ không bật sẵn**.

### 9 Trụ cột giá trị (v1.5):
1. **Bắt đầu dễ, từ hai điểm xuất phát:** `proofline start` tự nhận biết bạn bắt đầu **từ ý tưởng** (luồng A: phỏng vấn → spec có acceptance journey → kiến trúc → walking skeleton) hay **từ repo có sẵn** (luồng B: `proofline assess` → kiểm kê tính năng → khóa hành vi bằng test). Repo được đưa qua các **mức sẵn sàng R0–R4**; quyền tự chủ không bao giờ vượt quá mức sẵn sàng.
2. **Golden Set & Decision Replay Set:** Một bộ đo năng lực giải bài (bug lịch sử, bug cấy synthetic khi lịch sử còn ít, và việc đã xử lý tích lũy dần; đều có test ẩn), một bộ đo độ giống người khi ra quyết định (PR lịch sử + quyết định thật). Mọi thay đổi model, harness, playbook, taste đều phải chạy lại.
3. **Model Tiering theo vai trò (không cố định model):** Mặc định tạm thời: Haiku 5.5 cho triage và tác vụ nhanh, Sonnet 5.5 cho viết code, Opus 5.5 cho review và Lead Agent. Đổi qua config, override theo repo hoặc biến môi trường.
4. **Verification do harness thực thi:** Test tái hiện phải fail đúng ở assertion trên commit gốc và pass sau khi sửa; không sửa/skip test cũ; không regression; không tăng cảnh báo lint/type.
5. **User Journey & Acceptance Verification:** Test xanh chưa đủ. Agent tự khởi chạy app trong sandbox và đi hết luồng người dùng (API, CLI, giao diện); kịch bản phải fail trên bản lỗi và pass sau khi sửa. Với tính năng mới, acceptance journey được người duyệt trước trong spec và agent không được sửa.
6. **Adversarial Reviewer & Risk Score:** Agent thứ hai audit diff và kịch bản người dùng; mỗi PR có điểm rủi ro 0–10 tính bằng code xác định.
7. **Lead Agent & gu Tech Lead:** Gu được khởi tạo bằng phỏng vấn ngay ngày đầu; Lead Agent đặt câu hỏi Tech Lead sẽ hỏi, quyết định "good enough" hay làm lại; học tiếp từ chỉnh sửa, comment và lý do từ chối của người vào `tech-lead-taste.md`, có kiểm chứng.
8. **Bậc thang tự chủ:** Cấp 1 Draft PR → Cấp 2 tự merge việc rủi ro thấp vào `develop`/`staging` → Cấp 3 tự tìm việc và tự xử lý CI fail/lỗi staging. Lên cấp khi quyết định của agent đã trùng với người và kết quả thực tế tốt; tự hạ cấp khi có sự cố.
9. **Outer Loop:** Lắng nghe error tracker, ticket hỗ trợ, kênh cộng đồng và thảo luận của team; gom cụm nỗi đau, **tái hiện trước khi thành issue**, đo tần suất lỗi và phàn nàn trước/sau release để biết vấn đề đã thật sự được giải quyết chưa. Nội dung bên ngoài luôn là dữ liệu không đáng tin, dữ liệu cá nhân được che, và tin nhắn gửi khách luôn do người gửi.

---

## 2. Cấu trúc thư mục dự án

```
Proofline/
├── README.md                          # Tổng quan dự án & Chỉ dẫn điều hướng
├── docs/
│   ├── 01-research-harness2.md        # Nghiên cứu Google Research Harness²
│   ├── 02-research-lauren-tan-swarm.md# Kiến trúc Swarm & Verification của Lauren Tan
│   ├── 03-system-architecture.md      # Thiết kế kiến trúc kỹ thuật v1.5
│   ├── 04-execution-plan.md           # Kế hoạch thực thi 8 giai đoạn (Phase 0–7)
│   ├── 05-starting-points.md          # Hai điểm xuất phát & mức sẵn sàng R0–R4
│   ├── 06-the-outer-loop.md           # Outer Loop: khách hàng, team, telemetry production
│   └── 07-acp-pandacode.md            # Giao diện: Proofline là agent ACP, PandaCode là client đầu tiên
├── src/proofline/                     # Mã nguồn công cụ
│   ├── templates/                     # Bản mẫu chép vào workspace khi `proofline workspace init`
│   │   ├── proofline.yaml             # Config mẫu: model, ngân sách, gate, taste, autonomy (chưa có repo đích)
│   │   └── playbooks/                 # guardrails.md, tech-lead-taste.md, skills/bug-fix-playbook.md
│   ├── cli.py                         # Lệnh `proofline`
│   ├── config.py                      # Đọc/kiểm tra config, chọn model theo vai trò
│   ├── workspace.py                   # Tạo workspace từ bản mẫu
│   ├── assess.py, proc.py             # Đo mức sẵn sàng; chạy lệnh của repo đích
│   ├── autonomy.py                    # Mức tự chủ hiệu lực (max, mức sẵn sàng, kill switch)
│   ├── levels.py                      # Thang L0–L4 và R0–R4
│   └── state/                         # SQLite state/proofline.db + schema.sql
│                                      # (dự kiến) start, assess, interview, init, eval_runner, worker, sandbox,
│                                      #           verifier, journey_runner, dispatcher, decision_log
└── tests/
```

---

## Bắt đầu sử dụng

Proofline là **công cụ độc lập** với repo bạn muốn tích hợp. Repo công cụ không chứa cấu hình của repo đích nào; mọi thứ của bạn nằm trong một **workspace** riêng:

```
proofline-workspace/                   # Thư mục bất kỳ, nên là repo git riêng (private) của bạn/team
├── proofline.yaml                     # Config: repo đích, model, ngân sách, mức tự chủ...
├── playbooks/                         # Guardrails, gu Tech Lead, skill: chỉnh và tự học ở đây
├── evals/golden-set/<repo>/           # Golden Set theo từng repo đích
├── evals/decision-replay/<repo>/
├── repos/<repo>/                      # Bản clone của repo đích (không commit)
├── targets/<repo>/target.yaml         # Cấu hình từng repo đích (tự dò khi thêm) + artifacts nếu chọn workspace
├── state/proofline.db                 # Trạng thái chạy (không commit)
└── logs/
```

**Bắt đầu nhanh (2 lệnh):**

1. Cài công cụ:

```bash
uv tool install git+https://github.com/ThangLuuQuoc/Proofline
```

2. Tạo workspace, clone repo, tự dò cấu hình và đo mức sẵn sàng trong một lệnh:

```bash
proofline start https://github.com/<owner>/<repo> --workspace ~/proofline-workspace
```

`proofline start` tạo workspace nếu chưa có, clone repo vào `repos/<tên>/`, tự dò stack và lệnh (Node: npm/pnpm/yarn/bun, cổng dev server, thư mục Playwright; Python: uv/poetry/pip, pytest, ruff, mypy; Go; Rust), ghi `targets/<tên>/target.yaml` rồi chạy `assess` kiểm tra tĩnh. Hãy mở `target.yaml` kiểm tra trước khi chạy đầy đủ:

```bash
proofline assess <tên repo> --run
```

**Từng bước** (khi đã có workspace, chạy trong thư mục workspace):

| Lệnh | Việc làm |
| :--- | :--- |
| `proofline workspace init <thư mục>` | Tạo workspace trống từ bản mẫu |
| `proofline target add <git-url \| đường dẫn> [--name] [--branch]` | Thêm repo đích: clone nếu là URL, dùng tại chỗ nếu là thư mục có sẵn; tự dò cấu hình |
| `proofline target list` | Liệt kê repo đích |
| `proofline assess <tên repo> [--run]` | Đo mức sẵn sàng |

Repo đích cũng có thể khai báo thẳng trong `target_repos` của `proofline.yaml`; tên không được trùng với repo trong `targets/`.

Lệnh tìm config theo thứ tự: `--config` → biến `PROOFLINE_CONFIG` → `./proofline.yaml`. Mọi đường dẫn trong config tính theo thư mục chứa `proofline.yaml`, không theo thư mục đang đứng.

---

## Phát triển

Cần [uv](https://docs.astral.sh/uv/) và Python ≥ 3.11.

```bash
uv sync
```

```bash
uv run pytest -q
```

```bash
uv run ruff check . && uv run ruff format --check .
```

Các lệnh đã có:

| Lệnh | Việc làm |
| :--- | :--- |
| `proofline --version` | In phiên bản |
| `proofline start <git-url \| đường dẫn> [--workspace] [--run]` | Bắt đầu nhanh: tạo workspace nếu cần, thêm repo đích, đo mức sẵn sàng |
| `proofline workspace init [thư mục]` | Tạo workspace từ bản mẫu (không ghi đè file có sẵn) |
| `proofline target add <git-url \| đường dẫn>` / `proofline target list` | Thêm repo đích (clone + tự dò cấu hình) / liệt kê |
| `proofline config check` | Kiểm tra `proofline.yaml` của workspace hợp lệ |
| `proofline config model <vai_trò> [--repo <tên>]` | Model sẽ dùng cho vai trò (ưu tiên: `PROOFLINE_MODEL_<VAI_TRÒ>` → override theo repo → mặc định) |
| `proofline autonomy show --readiness <R0–R4>` | Mức tự chủ hiệu lực của mọi loại quyết định |
| `proofline db init [--path <file>]` | Tạo `state/proofline.db` trong workspace và áp schema |
| `proofline assess <repo> [--run] [--no-save]` | Đo mức sẵn sàng R0–R4 của repo đích, liệt kê việc cần làm để lên mức tiếp theo. Không có `--run` chỉ kiểm tra tĩnh; có `--run` sẽ build container, cài đặt, chạy test, khởi chạy app và hỏi trạng thái CI |
| `proofline spec approve <manifest.yaml> [--note]` | Người duyệt acceptance journey: ghi git blob của journey vào manifest (`.proofline/specs/<tên>.yaml`) và vào bảng `decisions` |
| `proofline verify <manifest.yaml> [--runs N] [--no-install]` | Chạy các gate của task trên nhánh, so với base (bản clone riêng trong workspace): journey không đổi so với bản duyệt, journey fail ở khẳng định trên base và pass trên nhánh, không sửa/skip test cũ, có test mới, cả bộ test N lượt mỗi bên so **danh sách test fail** (nhất quán = regression, chỉ vài lượt = không ổn định, có sẵn trên base = bỏ qua), typecheck/lint không xấu hơn base. Báo cáo `.md` + `.json` trong `<artifacts>/verify/`. Exit 1 khi có gate fail |
| `proofline acp [--workspace <thư mục>]` | Chạy Proofline như một agent ACP qua stdio (PandaCode và client ACP khác); hiện có `/assess`, `/status`, `/help` ([docs/07](docs/07-acp-pandacode.md)) |

Chạy lệnh trong môi trường dự án bằng `uv run proofline ...`.

---

## 3. Kiến trúc luồng hoạt động

```mermaid
flowchart TD
    Start["proofline start"] --> Track{"Điểm xuất phát"}
    Track -- "Ý tưởng (R0)" --> TrackA["Luồng A: phỏng vấn → spec + acceptance<br>→ kiến trúc → walking skeleton"]
    Track -- "Repo có sẵn (R1)" --> TrackB["Luồng B: assess → kiểm kê tính năng<br>→ characterization test + CI"]
    TrackA & TrackB --> Issue
    Issue["1. Nguồn việc<br>(proofline:ready · backlog từ spec · CI fail · lỗi staging · tự tìm)"] --> Triage["2. Triage (Haiku 5.5)<br>kích thước, loại task, task_class"]
    Triage -- "S/M, đủ rõ" --> Queue["3. Task Queue & Budget Watcher<br>($3/task, $50/đêm - hiệu chỉnh sau Phase 0)"]
    Triage -- "L hoặc mơ hồ" --> Skip["Ghi nhận / hỏi lại theo mức tự chủ"]

    subgraph Isolation ["4. Sandbox: Container + Worktree + Instance App"]
        Queue --> W1["Worker 1"]
        Queue --> W2["Worker 2"]
        Queue --> Wn["Worker N"]
    end

    subgraph WorkerCore ["5. Worker (Claude Code headless)"]
        direction TB
        LeadQ["Lead Agent: câu hỏi đầu tiên"] --> FastTier["Haiku 5.5: đọc code & viết repro test"]
        FastTier --> HeavyTier["Sonnet 5.5: viết patch + kịch bản người dùng"]
    end

    W1 & W2 & Wn --> WorkerCore
    WorkerCore --> Verifier["6. Verifier do harness thực thi<br>code gates + User Journey / Acceptance (fail trên base → pass)"]
    Verifier -- "Chưa đạt" --> WorkerCore
    Verifier -- "Đạt" --> Reviewer["7. Adversarial Reviewer (Opus 5.5)<br>+ Risk Score 0-10"]
    Reviewer -- "Request changes" --> WorkerCore
    Reviewer -- "Approve" --> Lead["8. Lead Agent (Opus 5.5)<br>good enough hay làm lại?"]
    Lead -- "Làm lại" --> WorkerCore
    Lead -- "Chấp nhận" --> Controller{"9. Autonomy Controller<br>(code xác định)"}
    Controller -- "Đủ điều kiện Cấp 2" --> AutoMerge["Tự merge vào develop/staging"]
    Controller -- "Chưa đủ" --> PR["PR draft kèm evidence"]
    AutoMerge & PR --> Digest["10. Morning Digest<br>duyệt/từ chối kèm lý do, phủ quyết"]
    Verifier -. "Thất bại sau 3 lần" .-> CircuitBreaker["Circuit Breaker<br>(giữ nhánh proofline/attempt-id & bàn giao)"]

    Digest -- "Quyết định của người + kết quả thực tế" --> DecisionLog[("Nhật ký quyết định")]
    DecisionLog -- "Lên/xuống mức tự chủ" --> Controller
    Readiness[("Mức sẵn sàng R0-R4<br>proofline assess mỗi đêm")] -- "Trần tự chủ" --> Controller
    Digest -- "Chỉnh sửa, comment, lý do, /proofline teach" --> Learning["11. Learning Loop<br>đề xuất taste/playbook dạng PR"]
    Learning --> Evals{"Golden Set + Decision Replay<br>không giảm điểm?"}
    Evals -- "Có + người duyệt" --> Playbooks[("tech-lead-taste.md<br>+ Playbooks & Skills")]
    Playbooks --> WorkerCore & Lead

    subgraph Outer ["12. Outer Loop (Phase 7)"]
        Ext["Error tracker · ticket · cộng đồng · thảo luận team"] --> Reader["Reader Agent không công cụ<br>che PII · gắn cờ injection"]
        Reader --> Cluster["Gom cụm nỗi đau"]
        Cluster --> ReproQ{"Tái hiện được?"}
        Monitor["Release Monitor<br>(release do người)"]
        Monitor -- "resolved" --> Reply["Nháp trả lời khách<br>(người gửi)"]
    end
    ReproQ -- "Có + người xếp ưu tiên" --> Issue
    AutoMerge & PR -.-> Monitor
    Monitor -- "Tần suất lỗi/phàn nàn trước/sau" --> DecisionLog
```

---

## 4. Tài liệu liên kết (Quick Links)

* Nghiên cứu Harness²: [01-research-harness2.md](docs/01-research-harness2.md)
* Case study Lauren Tan: [02-research-lauren-tan-swarm.md](docs/02-research-lauren-tan-swarm.md)
* Thiết kế kiến trúc hệ thống v1.5: [03-system-architecture.md](docs/03-system-architecture.md)
* Lộ trình triển khai 8 giai đoạn: [04-execution-plan.md](docs/04-execution-plan.md)
* Hai điểm xuất phát & mức sẵn sàng: [05-starting-points.md](docs/05-starting-points.md)
* Outer Loop: [06-the-outer-loop.md](docs/06-the-outer-loop.md)
* Tích hợp ACP với PandaCode (board, duyệt, bot, GitHub): [07-acp-pandacode.md](docs/07-acp-pandacode.md)
* Config mẫu: [proofline.yaml](src/proofline/templates/proofline.yaml)
* Chốt an toàn (mẫu): [guardrails.md](src/proofline/templates/playbooks/guardrails.md)
* Gu Tech Lead (mẫu): [tech-lead-taste.md](src/proofline/templates/playbooks/tech-lead-taste.md)
