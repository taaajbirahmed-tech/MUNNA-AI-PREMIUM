# MUNNA AI PREMIUM V1.1 — $0 starter

এই প্যাকেজে আছে:
- Telegram signal bot (`bot.py`)
- Android-friendly browser dashboard (`dashboard/`)
- Read-only dashboard API (`dashboard.py`)
- SQLite paper history

## গুরুত্বপূর্ণ সীমাবদ্ধতা

এই build **automatic live signals চালু করে না** যতক্ষণ না একটি যাচাইকৃত live data provider configure করা হয়। আগের V1 bot-এর live-stream adapter OTCharts-এর paid/authorized stream key ছাড়া চালু হবে না। Free tier-এ live OTC streaming নিশ্চিত নয়। Real forex/crypto feed-কে Quotex OTC বলে দেখানো হবে না। তাই feed না থাকলে bot WAIT/disabled থাকবে।

এটি এখন demo/paper-first; auto trade execution নেই এবং 1-minute ফলাফল নিশ্চিত করে না।

## Windows setup

1. Python 3.11+ ইনস্টল করো।
2. `@BotFather`-এ `/newbot` দিয়ে Telegram bot তৈরি করো। Token শুধু নিজের PC-তে `.env` ফাইলে বসাবে; আমাকে বা কাউকে পাঠাবে না।
3. `.env.example` কপি করে `.env` বানাও এবং `TELEGRAM_BOT_TOKEN` বসাও।
4. `run_bot_windows.bat` চালাও; Telegram-এ bot-কে `/start` পাঠাও।
5. আলাদা terminal-এ `run_dashboard_windows.bat` চালাও। PC-তে `http://127.0.0.1:8000` খুলবে।
6. Android ফোন ও PC একই Wi-Fi-তে থাকলে PC-এর local IPv4 address জেনে ফোনের browser-এ `http://PC-IP:8000` খোলো। উদাহরণ: `http://192.168.1.5:8000`। Windows Firewall চাইলে Private network access allow করো।

এই local dashboard Android browser-এ কাজ করবে; HTTPS hosting না থাকায় এটি এখনো installable Play Store APK/PWA নয়।

## Telegram commands

`/start`, `/status`, `/report`, `/history`, `/pause`, `/resume`, `/help`.

## Next stage

একটি যাচাইকৃত data source নির্বাচন ও পরীক্ষা করতে হবে—বিশেষ করে Quotex OTC ticks-এর সঙ্গে chart quote মেলে কি না। Provider-এর API key/token কখনো chat-এ দেবে না। Live data না থাকলে কোনো signal তৈরি হবে না।
