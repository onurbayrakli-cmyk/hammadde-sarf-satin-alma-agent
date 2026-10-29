# Yeni bilgisayar ve geliştirici için devir

Bu devir yalnız hammadde ve sarf satın alma agentını kapsar. Uygulama sürümü **0.3.0**; ilk hedef Mac mini/macOS üzerinde şirket içi, insan onaylı pilottur.

## Kaynağı edinme

GitHub üzerinden çalışma için özel repoya erişimi olan bir GitHub hesabı gerekir. Alıcı hesabı ve izin düzeyi doğrulanır, davet kabul edildikten sonra repo klonlanır. Özel repo URL'sinin paylaşılması veya davetin gönderilmesi erişimin kabul edildiği anlamına gelmez. Hesabı olmayan alıcı kurulum ZIP'ini ayrı alabilir; GitHub hesabı açılınca erişim ayrıca düzenlenir.

Repo GitHub ZIP'i olarak indirildiyse de kök `README.md` ve `app/` aynı yapıdadır. ZIP'in klasör adını kaynak sürümü veya kurulum kabulü sanmayın. Tam iş veritabanı/ayar yedeği bu kaynak devir paketine dahil değildir.

## İlk okuma ve doğrulama

1. Repo kökündeki `AGENTS.md`, `README.md` ve bu belgeyi okuyun.
2. Uygulama komutları için **app/** klasörüne geçin. Tam rehber `app/README.md`, AI kurulum görevi `app/Kurulum_Gorevi.md` içindedir.
3. Mevcut araçları ve çalışan servis/portları kontrol edin. Başka agentı durdurmayın veya onun ortamına kurulum yapmayın.
4. uv/Python mevcutsa `app/` içinde `PYTHONDONTWRITEBYTECODE=1 uv run --no-project --python 3.12 python transfer/verify_package.py .` çalıştırın. Manifest yalnız app içeriğini doğrular; `.git` repo kökünde kalır.
5. Ayrı Python ortamını ve repo dışındaki kalıcı config/state dizinlerini ayrıntılı rehberle hazırlayın. İlk bağımlılık kurulumu için internet gerekir.
6. Üç çevrimdışı test grubunu çalıştırın; gerçek test sayılarını ve sürümleri kaydedin. Tarihli kaynak sonucu yeni hedef sonucu yerine geçmez.
7. Kimlik doğrulamalı Langflow'u yalnız loopback'te açın, hedefe hazırlanmış akışı yeni UUID ile içe aktarın. Kaynak bilgisayarın UUID'sini yeniden kullanmayın.
8. `app/Kabul_Kaydi_Sablonu.md` kopyasını repo dışındaki kabul dizinine alın; gerçek hedef sonuçlarını burada tutun.

Codex/Claude'a verilecek başlangıç görevi:

> Bu reponun AGENTS.md, README.md ve docs/DEVIR.md dosyalarını oku. app/ içindeki satın alma agentını bu bilgisayarda ayrı, insan onaylı pilot olarak kur. app/README.md ve app/Kurulum_Gorevi.md sırasını izle. Önce kaynak bütünlüğünü ve mevcut ortamı doğrula. Sırları sohbete isteme, başka agentı değiştirme. SAP/posta/modeller/yeniden başlatma kabullerini ayrı kaydet; eksik bağlantılar varken canlı başarı varsayma.

## SAP, e-posta ve modeller

- [SAP rehberi](../app/sap/README.md), [BT teslim notu](../app/sap/BT_Teslim_Notu.md) ve alan sözleşmesi üzerinden gidilir. VPN, kurum endpoint'i/client'ı, CA ve kimlik ayrı güvenli kanaldan sağlanır; repo bunları içermez.
- Kaynak DEV GUI'de metadata/tek kalem okuması doğrulanmıştır. Hedef Python adapterinin TLS/kimlik/kodlama kabulü ve 12 alan eşlemesi, talep sahibi, durum ve dar iş kapsamı henüz ayrı tamamlanacaktır. Kısmen siparişli kaynak araştırmaya alınmaz; ham API anahtarı dönüştürülmez.
- Ayrı satın alma/bot kutusu hazır değilse boş kalır. Kişisel Outlook kullanılmaz; otomatik posta okuyucu/gönderici bu teslimde tamamlanmamıştır.
- OpenAI ve Anthropic API erişimleri kullanıcı tarafından hedefteki güvenli yerel alanlardan sağlanır. Sohbet aboneliği veya dolu anahtar alanı API başarısı sayılmaz. Küçük gerçek ortak koşu ve kota ayrıca sınanır.

Tüm dış yazışmalar taslaktır; insan incelemesi olmadan gönderim, fiyat/vade kabulü, sipariş veya SAP iş verisi yazma yapılmaz. Zamanlayıcı, çok kullanıcılı onay paneli, uzaktan erişim, uyku/VPN, reboot ve yedekten dönüş ayrı işletim kabulüdür.

## Geliştirmeye devam ederken

`app/`, bu devirde değiştirilmemiş 0.3.0 kaynak paketidir. İşlevsel değişiklik ayrı dalda, açık kapsamla yapılır; sürüm ve hash envanteri değişikliklerle birlikte yeni teslim olarak doğrulanır. Eski manifesti doğrulama hatasını gizlemek için değiştirmeyin; yeni kaynak sürümü, temiz testler ve dosya kapsamını birlikte inceleyin. Gerçek kuyrukla test veya ayarları Git'e alma yapılmaz.

Yedekleme, şema uyumu ve geri dönüş sırası [ayrıntılı README](../app/README.md) ile [teknik kurulum rehberinde](../app/Kurulum_ve_Guncelleme.md) bulunur. Kaynak ZIP'i veri yedeği değildir. Doğrulama kaydına yalnız gerekli teknik sonuçlar yazılır; sır, kişisel e-posta veya gerçek teklif/iş içeriği eklenmez.
