# Claude başlangıcı

Önce kök [AGENTS.md](AGENTS.md), [README.md](README.md) ve [docs/DEVIR.md](docs/DEVIR.md) dosyalarını oku. Uygulama ve kurulum yönergeleri `app/` içindedir; ayrıca `app/AGENTS.md` kurallarını uygula.

Bu repo yalnız hammadde/sarf satın alma pilotudur. Diğer agentları, kişisel Outlook'u, mevcut iş veritabanlarını veya önceki bilgisayarın akış UUID'sini kullanma. Gerçek ayar/veri ve sırlar repo dışında kalır.

0.3.0 kaynak teslimi, hedef bilgisayar kurulumu ve gerçek SAP/posta/model kabulü ayrı durumlardır. Önce `app/` içinde manifest ve sentetik testleri doğrula; gerçek gönderim, sipariş veya SAP'ye yazma başlatma. Bu devirde işlevsel uygulama kaynakları değiştirilmez. Yeni geliştirme ayrı sürüm ve temiz test/manifest teslimi gerektirir.
