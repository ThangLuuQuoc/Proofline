# Nghiên cứu chuyên sâu: Google Research Harness²

> **Tên bài báo:** *Harness²: Recursive Agent Harnessing for an Open World*  
> **Nhóm tác giả:** Ruiyao Xu, Yanfei Chen, Zhongying CuiZhu, Bhavana Dalvi Mishra, Yifei Ming, Han Yu, Rujun Han, Chen-Yu Lee, Tomas Pfister (Google Cloud AI Research)  
> **Mã arXiv:** [arXiv:2610.14224](https://arxiv.org/abs/2610.14224) | **Source Repo:** [google-research/harness2](https://github.com/google-research/harness2)

---

## 1. Bối cảnh & Vấn đề giải quyết

Trong kỷ nguyên Agentic AI, hiệu năng của một Agent phụ thuộc rất lớn vào **Harness** (toàn bộ môi trường bao bọc xung quanh mô hình: system prompts, công cụ, hướng dẫn sử dụng công cụ, bộ nhớ, guardrails, scripts trợ lực).

Tuy nhiên, việc tối ưu Harness đang đối mặt với 2 rào cản lớn trong môi trường thực tế (Open-World):
1. **Thiếu Verifier ở Test-time:** Khi agent đối mặt với task mới chưa từng thấy, môi trường thường **không có sẵn ground-truth label** hoặc test suite để agent biết mình làm đúng hay sai.
2. **Kém khái quát (Hyper-task-specific):** Các phương pháp tự sửa đổi thông thường chỉ sửa tạm thời cho task hiện tại mà không biến kinh nghiệm đó thành kỹ năng dùng lại được cho các task sau.

---

## 2. Giải pháp của Harness²: Dual-Level Recursion

Harness² giải quyết bài toán trên bằng cách **đóng băng hoàn toàn trọng số mô hình (Frozen Model Weights)** và triển khai **2 tầng đệ quy tự hoàn thiện (Dual-Level Recursion)**.

```mermaid
graph TD
    subgraph TaskLevel ["Tầng 1: Task-Level Recursion (Online / Per-Task)"]
        P1["Propose: Đề xuất chỉnh sửa Harness"] --> P2["Probe: Kiểm tra phản xạ môi trường không cần Ground-Truth"]
        P2 --> P3["Compose: Hợp nhất chỉnh sửa an toàn"]
    end

    subgraph HarnessingLevel ["Tầng 2: Harnessing-Level Recursion (Cross-Task Continual Learning)"]
        Extract["Trích xuất bài học & Mẫu thành công"] --> UpdateLib["Cập nhật Continual Improvement Library"]
        UpdateLib --> FeedNext["Kế thừa cho Task tiếp theo"]
    end

    TaskLevel -->|Hoàn thành task| Extract
    FeedNext --> TaskLevel
```

### 2.1. Tầng 1: Task-Level Recursion (Propose – Probe – Compose)
* **Propose:** Phân tích yêu cầu bài toán và bối cảnh mã nguồn, đề xuất điều chỉnh harness (vd: viết một helper script kiểm tra cú pháp, thêm instruction vào prompt, tạo subagent phụ).
* **Probe (Contrastive Probing):** Chạy thử nghiệm đối chứng trong môi trường sandbox để đo lường độ ổn định và tính khả thi mà **không cần ground-truth label**. Kỹ thuật này phát hiện sớm các lỗi cú pháp, xung đột runtime hoặc ảo giác của LLM.
* **Compose:** Ghép những thay đổi vượt qua bước Probe vào harness chính thức để thực thi giải quyết task.

### 2.2. Tầng 2: Harnessing-Level Recursion (Continual Library)
* Khi task kết thúc thành công, hệ thống tự kích hoạt bước **Reflection**:
  - Những công cụ nào viết ra có tính tái sử dụng?
  - Quy tắc nào đã giúp agent tránh lỗi lặp lại?
* Các kết quả này được chuẩn hóa thành tệp markdown/script và lưu vào thư viện tri thức chung (`Continual Improvement Library`).

---

## 3. 8 Thành phần Harness được mô đun hóa dạng File (File-Mounted Components)

Harness² thiết kế toàn bộ hạ tầng điều khiển thành 8 tệp/thư mục có thể đọc và sửa đổi trực tiếp:

| STT | Thành phần | Mô tả kỹ thuật |
| :--- | :--- | :--- |
| 1 | **System Prompt** | Persona, phong cách tư duy, thứ tự ưu tiên xử lý. |
| 2 | **Guardrails** | Các chốt chặn an toàn (vd: cấm force push, cấm xóa file cấu hình gốc, giới hạn số bước lặp). |
| 3 | **Memory** | Bộ nhớ ngắn hạn (theo task) và dài hạn (semantic cache, episodic memory). |
| 4 | **Tool Guidance** | Tài liệu hướng dẫn agent khi nào nên và không nên gọi từng công cụ cụ thể. |
| 5 | **Subagents** | Khai báo các agent con chuyên biệt (vd: reviewer, migration specialist, linter fixer). |
| 6 | **Skills** | Quy trình thực hiện task từng bước (playbooks/SOPs). |
| 7 | **Scripts** | Các file mã Python/Bash bổ trợ do agent tự viết ra để tự chạy trong môi trường. |
| 8 | **Middleware Plugins** | Các hook can thiệp vào trước và sau khi gọi model/tool để lọc dữ liệu hoặc log. |

---

## 4. Kết quả thực nghiệm

Trên các benchmark phức tạp mô phỏng môi trường làm việc thực tế của kỹ sư phần mềm:
* **WorkBuddy-Bench, JobBench, LAB:** Cải thiện trực tiếp từ **+4 đến +11 điểm %** tỷ lệ hoàn thành tác vụ thành công.
* Toàn bộ cải tiến đạt được mà **không cần bất kỳ GPU training hay fine-tune nào**, chỉ thuần túy tối ưu cấu trúc Harness.
