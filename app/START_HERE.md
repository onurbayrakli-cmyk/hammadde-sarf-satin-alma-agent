# Mac mini'ye AirDrop ile kurulum · 0.3.0

Bu teslim yalnız **hammadde ve sarf satın alma agentı** içindir. Diğer projelerin agentları, veritabanları ve otomasyonları taşınmaz. Hedef şirket içi insan onaylı pilottur; ZIP hazırlanması hedef kurulumun tamamlandığı anlamına gelmez.

1. ZIP, SHA-256 dosyası ve AirDrop başlangıç rehberini aynı Mac mini'ye AirDrop ile aktarın. Ayrıntılı README ve bağlı belgeler ZIP'in içindedir.
2. Hedefte ZIP hash'ini teslim kaydıyla karşılaştırın; yeni boş sürüm klasörüne çıkarın. Eski kurulumun üstüne açmayın.
3. **[README.md](README.md)** dosyasını açın. Ayrıntılı kurulum, kabul listesi, sorun giderme, yedek ve geri dönüş burada anlatılır.
4. Claude/Codex'e çıkarılan paket klasörünü açtırıp **[Kurulum_Gorevi.md](Kurulum_Gorevi.md)** görevini verin. Önce `AGENTS.md` okunur; hedef kabul kaydı paket dışında tutulur.
5. Önce paket/test/kimlik doğrulamalı yerel akış kurulumunu tamamlayın. Gerçek SAP, bot posta, iki-model koşusu, yeniden başlatma ve uzak erişimi ayrı ayrı kabul edin.

**Bu ZIP bağımlılıkları içeren çevrimdışı kurucu değildir.** Python/uv ve Langflow bağımlılıkları hedefte kontrol edilir/kurulur; internet gerekir. Kurulum adayı Python 3.12 + Langflow 1.12.4'tür. Başka bir Mac'teki temiz kurulum başarısı paketin hazırlanmasından hareketle iddia edilmez.

Paket temiz 21 bileşen/28 bağlantılı akışı, kaynak/test dosyalarını, dış config taslaklarını ve SAP okuma/tanı araçlarını içerir. Hesaplar, anahtarlar, sertifikalar, gerçek iş verileri, kaynak Langflow veritabanı ve çalışır sanal ortam dahil değildir. Ayrıntılı envanter `MANIFEST.json` içindedir.

0.3.0, bağlantısız tam metadata incelemesine ek olarak sınırlı SAP `schema` / `sample` tanı araçlarını taşır. Kaynakta SAP DEV GUI üzerinden metadata ve kaynak API anahtarıyla tek kalem okuması doğrulandı; örnek kısmen siparişli olduğu için kuyruğa alınmadı. Hedef Mac'in bağımsız adapter/TLS/kimlik ve iş sözleşmesi kabulü açık kalır. Eşlemeler ve kapsam/yetki kapıları tamamlanmadan SAP alımı açılmaz. Gerçek kurumsal metadata ve PR bilgileri bu pakete konmaz.

Ayrı bot e-posta adresi henüz hazır değildir. Otomatik posta okuyucu/gönderici, bağımsız zamanlayıcı ve 1–5 kişilik satın alma onay paneli tamamlanmış değildir. Mevcut araştırma/pazarlık araçları insanın başlattığı pilot içindir. Yerel taslak gerçek gönderim veya satın alma değildir.

İlgili belgeler: [README.md](README.md), [Kurulum_ve_Guncelleme.md](Kurulum_ve_Guncelleme.md), [kabul formu](Kabul_Kaydi_Sablonu.md), [sürüm notları](SURUM_NOTLARI.md), [SAP rehberi](sap/README.md).
