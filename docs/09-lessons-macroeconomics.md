# Bài học từ dự án macroeconomics: tính hữu ích, người viết journey độc lập, chế độ nhẹ

> **Bối cảnh:** lần chạy thật đầu tiên của luồng A ([05-starting-points.md](05-starting-points.md) mục 4) trên một repo dựng từ đầu: [macroeconomics](https://github.com/ThangLuuQuoc/macroeconomics), ứng dụng vĩ mô Việt Nam (FastAPI + Postgres + Next.js), một người dùng. Bốn tính năng đi hết spec → duyệt → làm → `proofline verify`: bảng tổng quan, thẻ thị trường, giả lập kịch bản, kiểm chứng lịch sử (2026-10-27).
>
> **Trạng thái:** chế độ nhẹ (mục 5) đã có code; gate chỉ số nghiệp vụ (mục 3) và người viết journey độc lập (mục 4) còn là **thiết kế**.

---

## 1. Proofline đã giúp được gì

| Cơ chế | Chuyện đã xảy ra | Giá trị |
| :--- | :--- | :--- |
| `acceptance_unchanged` | Dữ liệu tổng hợp trong journey đã duyệt (thẻ thị trường) làm connector Pink Sheet lỗi, vì connector đòi có cột vàng | Không sửa được journey nên phải sửa code: connector đọc các cột có mặt, chỉ lỗi khi không còn cột nào, nên chịu được khi nguồn thật đổi định dạng |
| `acceptance_fails_before` | Một journey fail trên base vì Playwright chờ quá giờ, không phải vì tính năng thiếu | Gate từ chối, journey phải chờ đúng phần tử cần kiểm; nhờ đó có thêm `base_retries` |
| Spec + quyết định | Định dạng số kiểu Việt Nam, ngưỡng 0,8 so với "không đổi", thêm `statsmodels`, role chỉ đọc `macro_reader`, gu T-1..T-4 | Mọi lựa chọn của người có dấu thời gian và lý do; khi app hiện "chưa đủ dữ liệu" thì biết đó là quy tắc T-4 chứ không phải lỗi |
| `assess` | Bắt lỗi `docker build` (`pnpm build`) ngay ở walking skeleton, trước tính năng đầu tiên | Lỗi môi trường không lẫn vào gate của tính năng |
| Tinh thần "có bằng chứng" | Trang kiểm chứng và danh sách giả thuyết thị trường của app làm theo cùng nguyên tắc | Nguyên tắc của Proofline lan sang cách thiết kế sản phẩm |

## 2. Những gì Proofline không làm được

1. **Tám gate đều xanh nhưng tính năng chưa có ích.** Giả lập kịch bản qua toàn bộ gate, nhưng kiểm chứng lịch sử sau đó cho thấy mô hình không đoán tốt hơn "không đổi" (tỷ lệ MAE: xuất khẩu 1,03; sản xuất công nghiệp 1,42; PMI 1,56; CPI 1,97). Gate chứng minh "làm đúng như journey mô tả", không chứng minh "đúng" hay "có ích". → mục 3.
2. **Người làm tự kiểm chính mình.** Cùng một agent viết spec, journey và code. Người có duyệt journey, nhưng journey vẫn có thể có cùng điểm mù với code. → mục 4.
3. **Verify chưa từng chặn bản nào.** Bốn báo cáo đều pass ở lần chạy chính thức; lỗi được sửa trong lúc làm. Verify vẫn có giá trị làm bằng chứng, nhưng không phải nơi bắt lỗi chính.
4. **Lỗi dữ liệu quan trọng nhất do kiểm tay tìm ra:** CPI của nguồn trộn hai định nghĩa (trung bình từ đầu năm và so cùng kỳ), chuỗi GDP thực bị đứt năm 2026, dữ liệu IMF và Comtrade về Việt Nam đã cũ. Proofline chưa có chỗ nào cho loại kiểm tra này ngoài test do agent tự viết.
5. **Nặng với thay đổi nhỏ.** Sửa CI, sửa connector, thêm tài liệu đều không cần spec và journey, nhưng chưa có đường đi nào khác ngoài làm tay. → mục 5.

---

## 3. Gate chỉ số nghiệp vụ (`business_metric`)

**Mục tiêu:** spec khai báo được một thước đo "có ích" mà người duyệt cùng journey, và verify chạy nó như mọi gate khác.

### 3.1. Khai báo trong spec

```yaml
# .proofline/specs/multi-shock-model.yaml
metrics:
  - name: mae_ratio_exports
    command: uv run python -m macro.backtest --json      # in JSON ra stdout
    value: outcomes["vn.exports_yoy"].ratio              # đường dẫn tới số
    kind: fixture | live
    threshold: { max: 0.8 }                              # ngưỡng tuyệt đối
    vs_base: { not_worse_by: 0.02 }                      # không xấu hơn base quá mức này
    blocking: true
```

- **Hai loại dữ liệu:**
  - `fixture`: dữ liệu tổng hợp hoặc ảnh chụp cố định trong repo, kết quả xác định. Đây là loại chặn mặc định.
  - `live`: dữ liệu thật, thay đổi theo thời gian. Chạy base và nhánh trên **cùng một ảnh chụp dữ liệu** (cùng lần ingest), ghi mã ảnh chụp vào báo cáo. Mặc định chỉ báo cáo; chặn khi spec ghi `blocking: true`.
- **Người duyệt metric cùng journey.** Blob của file khai báo và của lệnh đo được ghi vào `journey_approvals`; agent đổi ngưỡng hay đổi cách tính là vi phạm, giống `acceptance_unchanged`.

### 3.2. Cách gate chạy

1. Chạy `command` trên base và trên nhánh; đọc `value`.
2. Pass khi giá trị của nhánh đạt `threshold` **và** không xấu hơn base quá `vs_base`. Base chưa có tính năng (lệnh đo lỗi trên base) thì chỉ xét `threshold`.
3. Báo cáo ghi giá trị base, nhánh, ngưỡng, mã ảnh chụp dữ liệu; mỗi giá trị thành một fact `metric.<name>@<commit>` trong kho tri thức ([08-domain-knowledge.md](08-domain-knowledge.md)) để thấy xu hướng qua các lần đổi.
4. Không đạt mà người vẫn muốn ship (vd ra mắt dưới nhãn "thử nghiệm") → quyết định `metric_waiver`, mức trần L1, bắt buộc có lý do. Không bao giờ tự động.

### 3.3. Khi nào bắt buộc

- Spec khai báo `kind: model` (dự báo, xếp hạng, gợi ý, chấm điểm) **phải** có ít nhất một metric; thiếu thì `proofline spec approve` từ chối.
- Tính năng khác: không bắt buộc, gate ghi `not_applicable`.

**Với macroeconomics:** nếu có gate này, giả lập kịch bản v1 đã bị chặn trên dữ liệu thật, và việc ra mắt nó như một bản thử nghiệm sẽ là một quyết định `metric_waiver` có ghi lại, thay vì phát hiện sau khi đã merge. Mô hình nhiều cú sốc sẽ là spec đầu tiên dùng gate này.

---

## 4. Người viết journey độc lập

**Mục tiêu:** tiêu chí nghiệm thu không do chính người làm đặt ra.

- **Vai trò `acceptance_author`** viết journey (và metric) chỉ từ spec, **không đọc code đang làm**, chỉ dùng giao diện công khai (UI, API, CLI). Phiên mới, không chung lịch sử với worker; bàn giao qua artifact như [03-system-architecture.md](03-system-architecture.md) mục 2.4b.
- **Khác model hoặc khác harness** với worker khi có thể. Nếu chỉ có một model: ít nhất là phiên mới, và báo cáo ghi rõ "độc lập yếu".
- **Ghi dấu vân tay người viết** (model, harness, phiên) vào `journey_approvals`. Verify thêm gate `acceptance_independent`: so dấu vân tay người viết journey với worker làm tính năng.
  - Trùng nhau: ở mức tự chủ hiện tại chỉ **cảnh báo**; khi xét tự merge (Cấp 2) thì là điều kiện bắt buộc.
- **Người vẫn duyệt journey** (`acceptance_approval`, trần L1). Độc lập không thay người duyệt, chỉ giảm điểm mù chung.
- **Tuỳ chọn về sau:** chạy journey trên vài bản đột biến của code (bỏ một nhánh `if`, trả về hằng số); journey không bắt được đột biến nào là journey yếu, báo cho người duyệt.

---

## 5. Chế độ nhẹ (task type `chore`) — đã làm

**Mục tiêu:** thay đổi nhỏ không đổi hành vi người dùng vẫn có bằng chứng, nhưng không cần spec và journey.

```bash
proofline verify --chore [--base main] [--path <checkout>]   # không cần file spec
proofline verify .proofline/specs/<tên>.yaml                 # hoặc manifest có task_type: chore
```

- **Gate `chore_scope`, đo bằng diff so với base (agent không tự khai được).** Không đạt thì verify dừng ngay, không chạy test, và gợi ý chuyển sang `bug` hoặc `feature` (cần spec). Điều kiện:
  - tổng số dòng thêm + xoá ở file **không phải tài liệu** ≤ `verification.chore.max_diff_lines` (mặc định 60). Tài liệu (`*.md`, `*.rst`, `*.txt`, `docs/`) và báo cáo `.proofline/verify/` không tính, nên đổi chỉ tài liệu thì lớn mấy cũng được;
  - không chạm vùng nhạy cảm: `review.risk_score.sensitive_paths` (mẫu: auth, payment, migrations, `.github/workflows`), và luôn cả `.proofline/specs/**` (chỗ ghi bản duyệt của người);
  - không thêm lối vào mới cho người dùng: file trang/route mới (`app/**/page.tsx`, `app/**/route.ts`, `pages/**`) hoặc dòng thêm vào kiểu `@app.get(`, `@router.post(`, `@app.command(`, `@click.command`, `add_typer(`, `app.get('/...')`. Thêm mẫu riêng của repo qua `verification.chore.extra_entry_point_patterns` (regex).
- **Gate còn lại:** `test_integrity` (không sửa/xoá test cũ, không thêm skip), `no_regression`, `static_*`. Không đòi test mới.
- **Rẻ hơn:**
  - base chỉ được clone và cài khi thật sự cần;
  - kết quả test của base đã đo trên **đúng commit** (kho tri thức, ít nhất 2 lượt) được dùng lại: chỉ chạy nhánh, mặc định 1 lượt. Báo cáo ghi "đo trước, không chạy lại". Khi đó không biết test nào là mới, nên test mới fail bị tính như regression;
  - lần dùng lại không cộng thêm số đo vào kho tri thức (tránh đếm một lần đo hai lần).
- **Chưa làm:** điều kiện "không chạm lệnh đo metric" chờ gate `business_metric` (mục 3); CI xanh trên nhánh cho thay đổi workflow (hiện workflow là vùng nhạy cảm nên không đi chore).
- Không thay đổi quy tắc merge: chế độ nhẹ chỉ bớt thủ tục, không bớt quyền của người.

**Với macroeconomics:** các việc như sửa connector Pink Sheet cho bền hơn, thêm tài liệu giả thuyết thị trường sẽ đi đường này. Chạy thử 2026-10-27 trên nhánh lưu các báo cáo verify (6 file, 0 dòng code): lần đầu chạy base và nhánh 2 lượt, cả 5 gate pass; lần hai dùng lại kết quả base đã đo, chỉ chạy nhánh 1 lượt, xong trong khoảng 40 giây.

---

## 6. Thứ tự làm

1. **Chế độ nhẹ:** rẻ nhất, dùng được ngay hằng ngày.
2. **Gate `business_metric`:** cần cho spec mô hình nhiều cú sốc của macroeconomics.
3. **Người viết journey độc lập:** cần thêm vai trò và một model/harness thứ hai; làm trước khi bật tự merge.

## 7. Câu hỏi còn mở

1. Metric `live` chạy mất vài phút (backtest có hàng chục lần ước lượng): có cần cache theo (commit, ảnh chụp dữ liệu) không?
2. `kind: model` do người khai trong spec hay Proofline tự đoán (vd từ thư viện thống kê/ML trong diff)?
3. Kiểm tra chất lượng dữ liệu (định nghĩa chuỗi đổi, chuỗi đứt, dữ liệu cũ) có nên thành một loại mục trong kho tri thức theo domain, hay là test của repo đích?
