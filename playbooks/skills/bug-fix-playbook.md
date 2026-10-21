# Bug Fix Playbook (Quy trình chuẩn xử lý lỗi)

> **Loại task:** Bug Fix / Defect Remediation  
> **Áp dụng:** Mọi worker được phân công xử lý issue gắn nhãn `type:bug`

---

## Quy trình 5 bước bắt buộc

### Bước 1: Khám phá & Định vị (Locate)
1. Đọc kỹ mô tả trong Issue: xác định hành vi mong đợi (expected) và hành vi thực tế (actual).
2. Tìm kiếm các file nguồn, hàm hoặc class liên quan trực tiếp đến lỗi.
3. Kiểm tra git log gần đây xem có thay đổi nào gây ra lỗi này không.

### Bước 2: Tái hiện bằng Test Case (Reproduction Test - Bắt buộc)
1. Viết một test case mới (đặt tên `tests/repro_issue_<id>_test.py` hoặc tương đương).
2. Chạy test case này.
3. **Điều kiện tiên quyết:** Test case này PHẢI BỊ FAIL (chứng minh lỗi đã được tái hiện chính xác). Nếu test case pass ngay từ đầu, bạn đã viết sai test hoặc hiểu sai issue.

### Bước 3: Sửa đổi tối giản (Minimal Patch)
1. Viết code sửa đổi trực tiếp vào nguyên nhân gốc rễ (Root Cause).
2. **Quy tắc vàng:** Chỉ sửa lượng code tối thiểu cần thiết để test pass. Không nhân tiện refactor lại toàn bộ module hoặc sửa các phần không liên quan.

### Bước 4: Kiểm chứng đa tầng (Verification)
1. Chạy lại test case tái hiện: Đảm bảo test case chuyển sang **PASS (Xanh)**.
2. Chạy toàn bộ test suite cũ của dự án: Đảm bảo **Không có regression**.
3. Chạy linter & typechecker: Đảm bảo **0 warning mới**.
4. **Kiểm chứng như người dùng** (khi repo có `app_runtime` và lỗi ảnh hưởng hành vi người dùng):
   - Viết kịch bản trong `tests/journeys/issue_<id>` diễn đạt mục tiêu người dùng trong issue: gọi API, chạy lệnh CLI, hoặc thao tác giao diện.
   - Kịch bản phải có một **khẳng định mục tiêu** rõ ràng ở cuối; phải **fail trên bản lỗi** và **pass sau khi sửa**.
   - Không viết khẳng định lỏng chỉ để qua gate; harness sẽ tự chạy lại kịch bản và reviewer sẽ đối chiếu với issue.

### Bước 5: Báo cáo & Mở Pull Request
1. Commit code với thông điệp rõ ràng theo chuẩn Conventional Commits (vd: `fix(auth): handle null token gracefully (#142)`).
2. Tạo Pull Request với đầy đủ bằng chứng kiểm thử (test logs, diff summary) và bằng chứng người dùng (transcript, ảnh trước-sau) nếu có.
