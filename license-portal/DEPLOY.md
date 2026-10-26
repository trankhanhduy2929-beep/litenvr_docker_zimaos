# Deploy license portal: Vercel + Cloudflare D1

Portal là app Next.js riêng trong `license-portal`. Nó cấp license key, gắn key
với thiết bị LiteNVR, thu hồi/gia hạn, nhận webhook PayOS và quản lý tài khoản
mua hàng. Addon LiteNVR gọi portal qua `license.server_url` (mặc định
`https://litenvr-license.vercel.app`).

## 1. Tạo D1 + gateway

Cài Wrangler và đăng nhập Cloudflare:

```bash
npx wrangler login
npx wrangler d1 create litenvr-license-production
```

Cập nhật `database_id` trong `cloudflare/wrangler.jsonc` nếu tạo database mới,
rồi tạo bảng:

```bash
cd cloudflare
npx wrangler d1 execute litenvr-license-production --remote --file schema.sql
npx wrangler deploy
npx wrangler secret put GATEWAY_SECRET
```

`GATEWAY_SECRET` là chuỗi ngẫu nhiên; giá trị này cũng dùng cho
`D1_GATEWAY_SECRET` trên Vercel. Gateway chỉ nhận `POST /batch` có header
`Authorization: Bearer <secret>`. Client D1 tự gửi
`User-Agent: LiteNVR-License/1.0`; đừng chặn UA này bằng WAF.

## 2. Đưa code lên Vercel

Import repo LiteNVR vào Vercel, đặt **Root Directory** = `license-portal`.
Vercel chỉ build portal; không cần build LiteNVR.

## 3. Biến môi trường

Thêm cho Production (và Preview/Development nếu cần):

| Biến | Giá trị |
| --- | --- |
| `DATABASE_PROVIDER` | `d1` |
| `D1_GATEWAY_URL` | `https://<worker>.workers.dev/batch` |
| `D1_GATEWAY_SECRET` | trùng `GATEWAY_SECRET` của Worker |
| `LICENSE_SIGNING_SECRET` | chuỗi ngẫu nhiên ≥ 32 ký tự, dùng ký token license |
| `AUTH_SECRET` | tùy chọn; bí mật ký cookie phiên. Bỏ trống sẽ dùng `LICENSE_SIGNING_SECRET` |
| `ADMIN_TOKEN` | token mạnh cho API quản trị legacy |
| `NEXT_PUBLIC_PRICE_WEEKLY` | giá VND gói 7 ngày, ví dụ `50000` |
| `NEXT_PUBLIC_PRICE_LIFETIME` | giá VND vĩnh viễn, ví dụ `200000` |
| `NEXT_PUBLIC_BANK_NAME` | tên ngân hàng |
| `NEXT_PUBLIC_BANK_ACCOUNT` | số tài khoản |
| `NEXT_PUBLIC_BANK_OWNER` | chủ tài khoản |
| `NEXT_PUBLIC_SUPPORT_CONTACT` | Zalo/email hỗ trợ |
| `NEXT_PUBLIC_LITENVR_URL` | link repo/tài liệu LiteNVR |

Tạo secret ngẫu nhiên:

```bash
openssl rand -base64 48
```

Sau khi deploy, `GET /api/health` phải trả `{"status":"ok","db":"ok"}`.

## 4. Bật PayOS

Thêm `PAYOS_CLIENT_ID`, `PAYOS_API_KEY`, `PAYOS_CHECKSUM_KEY`,
`PAYOS_RETURN_URL` (ví dụ `https://<domain>/success`) và `PAYOS_CANCEL_URL`
(ví dụ `https://<domain>/?canceled=1`) vào Vercel, rồi deploy lại.

Đăng ký webhook:

```bash
curl -X POST https://api-merchant.payos.vn/confirm-webhook \
  -H 'Content-Type: application/json' \
  -H 'x-client-id: <PAYOS_CLIENT_ID>' \
  -H 'x-api-key: <PAYOS_API_KEY>' \
  -d '{"webhookUrl":"https://<domain>/api/payment/payos"}'
```

Webhook đã ký sẽ tự đánh dấu đơn `paid` và cấp key. Chưa cấu hình PayOS thì
portal chỉ dùng chuyển khoản thủ công (admin tạo key tay).

## 5. Tài khoản và chống lạm dụng

- Đăng ký/đăng nhập bằng email **hoặc** số điện thoại Việt Nam + mật khẩu ≥ 8 ký
  tự. Không dùng OTP.
- Cookie phiên `litenvr_session` ký HMAC, hạn 7 ngày, `HttpOnly`, `SameSite=Lax`.
- Một tài khoản chỉ nhận trial 1 ngày một lần; tối đa 3 trial/IP trong 24 giờ.
- Đăng nhập/đăng ký bị giới hạn theo IP và theo tài khoản trong `auth_events`.
- Đơn đã mua gắn với tài khoản; key hiển thị tại `/account`.

Tạo tài khoản admin (chạy một lần):

```bash
curl -X POST https://<domain>/api/admin/accounts \
  -H "x-admin-token: $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"identifier":"admin@example.com","password":"<mat-khau-manh>","role":"admin"}'
```

Sau đó đăng nhập tại `/admin` bằng email/SĐT + mật khẩu. Đổi mật khẩu tại
`/account`. `ADMIN_TOKEN` chỉ còn dùng cho API quản trị, không dùng để đăng nhập
giao diện.

## 6. Kích hoạt trên LiteNVR

1. Mở LiteNVR → **Cài đặt → Bản quyền**.
2. Mở link **Mua hoặc quản lý license** trên đầu hộp thoại.
3. Dán key khách đã mua, bấm **Kích hoạt**.
4. Bấm **Kiểm tra lại**. Trạng thái phải xanh.

Key gắn với **Mã cài đặt** của thiết bị. Khi đổi máy, vào `/admin`, bấm
**Gỡ máy** cho key đó rồi kích hoạt lại. Có thể cộng ngày bằng nút **+30 ngày**.

## 7. Kiểm tra nhanh

```bash
curl -s https://<domain>/api/health

curl -s -X POST https://<domain>/api/license/activate \
  -H 'Content-Type: application/json' \
  -d '{"key":"LITE-XXXX-XXXX-XXXX","installation_id":"test-device","version":"1.9.19"}'
```

Kết quả hợp lệ có `valid: true`, `plan`, `expires_at`, `token`. Nhớ gỡ máy test
trong `/admin` sau khi kiểm tra.

## 8. Vận hành

- **Gia hạn:** nút `+30 ngày` trong `/admin`, hoặc tạo key mới cùng gói.
- **Thu hồi:** đổi trạng thái `revoked`; addon mất quyền sau lần kiểm tra kế
  tiếp (tối đa 6 giờ) hoặc ngay khi bấm Kiểm tra lại.
- **Sao lưu D1:** dùng `wrangler d1 export` hoặc Time Travel của Cloudflare.
- **Ngoại tuyến:** addon dùng tiếp tối đa `grace_days` (mặc định 7 ngày) kể từ
  lần kiểm tra thành công cuối.
- **Bảo mật:** không commit `.env.local`; xoay `LICENSE_SIGNING_SECRET` sẽ làm
  phiên đăng nhập cũ mất hiệu lực. Toàn bộ API phải chạy HTTPS.

## 9. Chạy cục bộ

```bash
cd license-portal
cp .env.example .env.local
npm install
npm run dev
```

Mở <http://localhost:3000>, `/auth`, `/account`, `/admin`.

- D1: điền `D1_GATEWAY_URL` + `D1_GATEWAY_SECRET`, bảng tạo bằng
  `cloudflare/schema.sql`.
- MySQL dự phòng: đặt `DATABASE_PROVIDER=mysql`, điền `DB_*`, chạy `schema.sql`.
