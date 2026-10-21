# Tech Lead Taste (Gu kỹ thuật của Tech Lead)

> **Mục đích:** Lưu trực giác và "gu" của Tech Lead để Lead Agent và các worker ra quyết định giống người: hỏi gì đầu tiên, khi nào chấp nhận "good enough", khi nào phải làm lại, nguyên tắc kiến trúc nào không được phá.
>
> **Cách file này thay đổi:** Lead Agent đề xuất thay đổi dạng PR từ phản hồi review (chỉnh sửa trước khi merge, comment review, lý do trong digest, `/proofline teach`). Thay đổi chỉ được merge khi Decision Replay Set và Golden Set không giảm điểm, và có người duyệt.
>
> **Thứ tự ưu tiên:** `guardrails.md` > file này > playbook theo loại task. Nguyên tắc mâu thuẫn với guardrails bị loại.

---

## Định dạng một nguyên tắc

```markdown
### T-<số>: <Tên ngắn của nguyên tắc>
- **Trạng thái:** candidate | active | retired
- **Phạm vi:** <toàn dự án | thư mục/module | loại task>
- **Quy tắc:** <điều cần làm / không làm, viết thành hành động cụ thể>
- **Lý do:** <vì sao Tech Lead quyết định như vậy>
- **Bằng chứng:** <link PR/comment>, <link>, ... (số lần quan sát: <n>)
- **Nguồn:** interview | code_convention | human_edit | review_comment | digest_reason | teach
- **Loại:** declared (người tuyên bố: interview, teach) | observed (rút từ hành vi: chỉnh sửa, review)
- **Cập nhật lần cuối:** <YYYY-MM-DD>
```

Nguyên tắc chuyển `candidate` → `active` khi có ≥ 3 bằng chứng hoặc đến từ `/proofline teach` / `proofline interview`.

**Nói vs làm:** khi nguyên tắc `declared` mâu thuẫn với hành vi `observed` (vd: nói ưu tiên test tích hợp nhưng nhiều lần tự viết lại thành test đơn vị), Lead Agent không tự chọn mà đưa mâu thuẫn vào digest để người quyết.

Nguyên tắc không còn được quan sát hoặc bị người phủ nhận chuyển sang `retired`, không xóa.

---

## 1. Câu hỏi đầu tiên khi nhận một lỗi

> Lead Agent đặt các câu hỏi này trước khi worker bắt tay sửa, và ghi câu trả lời vào ngữ cảnh.

*(Chưa có nguyên tắc. Bootstrap bằng `proofline interview` khi bắt đầu, và `proofline taste mine` nếu repo có lịch sử review.)*

## 2. "Good enough" hay làm lại

> Tiêu chí Lead Agent dùng sau khi worker xong, trước khi PR đến tay người.

*(Chưa có nguyên tắc.)*

## 3. Nguyên tắc kiến trúc của dự án

> Tầng nào được gọi tầng nào, chỗ nào được đặt logic, ranh giới module.

*(Chưa có nguyên tắc.)*

## 4. Phong cách code & phạm vi thay đổi

> Mức độ sửa chấp nhận được, cách đặt tên, cách xử lý lỗi, mức độ comment.

*(Chưa có nguyên tắc.)*

## 5. Những thứ luôn bị từ chối

> Các mẫu mà Tech Lead đã nhiều lần yêu cầu sửa hoặc đóng PR.

*(Chưa có nguyên tắc.)*
