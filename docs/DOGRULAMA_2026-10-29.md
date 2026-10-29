# Kaynak repo doğrulaması · 29 Ekim 2026

Bu kayıt, kaynak bilgisayarda bağımsız repo klasöründeki **app/** üzerinden yapılan çevrimdışı kontroldür. Yeni bir Mac mini kurulumu veya canlı satın alma kabulü değildir.

- Uygulama kaynağı: 0.3.0; 88 dosya + MANIFEST.json.
- Kaynak arşiv SHA-256: `917d1d818387eabf2a265552229e42531d03d51c4c7a4bd8d1c98226ef95f8f7`.
- Arşiv, çıkarılmış uygulama ve manifest bütünlüğü doğrulandı. Uygulama dosyaları repo için değiştirilmedi.
- Repo kökündeki `.git` metadata'sı, `app/` tam envanter kontrolünün dışında kalır; doğrulayıcı değiştirilmedi.

| Test grubu | Sonuç |
|---|---:|
| Temel unittest | 372 geçti |
| Bileşen unittest | 416 geçti |
| Claude protokol pytest | 18 geçti |
| Toplam | **806 geçti** |

Testler gerçek config değişkenleri çıkarılmış süreçlerde, geçici sentetik verilerle çalıştırıldı. Bytecode yazımı kapatıldı; pytest cache sağlayıcısı kullanılmadı. Yeni bağımlılık kurulmadı, çalışan Langflow/SAP/posta/model işine müdahale edilmedi.

SAP alan/iş kapsamı/kişi/teknik hesap, hedef TLS/kimlik/adapter, gerçek ortak iki-model koşusu, bot kutusu, bağımsız zamanlayıcı, çok kullanıcılı onay paneli, reboot ve yedekten dönüş ayrı kabul maddeleridir. Üretime hazır, ileti gönderilmiş veya satın alma yapılmış olduğu iddia edilmez.

Yeni değişikliklerde bu tarihli kaydı yeni test başarısı saymayın. [Kök README](../README.md) test komutlarını, [devir belgesi](DEVIR.md) sonraki adımları açıklar.
