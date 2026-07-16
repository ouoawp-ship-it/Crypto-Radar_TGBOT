# v1.87.6 确定性 Certbot 生产验收

## 原因

服务器 Certbot 2.9.0 在 `renew --dry-run` 中会主动加入最长数分钟的随机等待。本次真实诊断记录到约 470 秒延迟，导致远程自动验收看似卡住，也解释了此前用户在等待期间继续粘贴命令的现象。

## 修复

- HTTPS 验收改用 `certbot renew --dry-run --no-random-sleep-on-renew`。
- 保留既有的失败输出尾部显示与诊断文件留存。
- 已在生产服务器的 Certbot 2.9.0 上直接验证参数兼容：模拟续期约 17 秒完成，`paoxx.com` 与 `www.paoxx.com` 均成功。

## 部署影响

不改变证书、续期定时器或业务服务，只移除 dry-run 验收中的随机等待。正式自动续期行为保持 Certbot 原配置。
