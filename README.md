# Hammadde ve Sarf Satın Alma Agentı

Hammadde ve sarf malzeme taleplerinde mevcut tedarikçilerden teklif toplama sürecini izleyen, Türkiye'de alternatif tedarikçiler araştıran, teklifleri karşılaştıran ve kaynaklı pazarlık taslakları hazırlayan Langflow uygulamasının bağımsız kaynak reposu.

**Durum:** Şirket içi, insan onaylı pilot kaynak teslimi. Uygulama kaynağı **0.3.0** sürümüdür. Repo oluşturulması; hedef bilgisayarın kurulduğu, gerçek bağlantıların çalıştığı veya satın alma yapıldığı anlamına gelmez.

## Kapsam

- Hammadde/sarf talebini ve ortak teklif şartlarını kaydetme; SAP veya ayrı bot e-postası girişine hazırlık.
- Her talep için sahibinden araştırmanın biteceği tarih, saat ve saat dilimini alma.
- Mevcut tedarikçiler için teklif isteği taslakları, yeni tedarikçi araştırması ve kaynaklı hammadde haberleri.
- Aynı kapsamlı tekliflerde fiyat, teknik özellik, teslim koşulu, KDV, vade farkı ve aylık oran karşılaştırması.
- Süre sonunda yönetici raporu taslağı; geç gelen teklifleri ayrı gösterme.
- En düşük uygun teklif üzerinden %1–2 indirim isteği; kaynaklı vade seçeneği varsa ayrı +30 gün seçeneği. Geçmiş alış ve tarihli kur/Brent kanıtı bulunursa gerekçeye dahil etme.
- OpenAI ve Anthropic modellerinin görevleri ile yerel kaynak/hesap denetimleri.

Fiyat, KDV, teknik özellik veya geçmiş alış uydurulmaz. Rakip isimleri/fiyatları dış yazışmaya konmaz. Çıktılar insan incelemesine sunulan yerel taslaklardır; otomatik e-posta gönderimi, ticari kabul, sipariş ve SAP iş verisi yazma bu teslimin işlemleri değildir.

## Repo düzeni

```text
README.md             Repo başlangıcı
AGENTS.md             Codex ve diğer kodlama agentları için kurallar
CLAUDE.md             Claude için başlangıç yönlendirmesi
docs/DEVIR.md         Yeni bilgisayar ve alıcı için devir sırası
SOURCE_PROVENANCE.json Kaynak arşivin sürümü ve SHA-256 köken kaydı
app/                  0.3.0 kaynak paketinin değiştirilmemiş içeriği
  README.md           Ayrıntılı Mac mini kurulum ve işletim rehberi
  MANIFEST.json       app/ içindeki dosyaların hash envanteri
  flow.json           Temiz Langflow akışı: 21 bileşen / 28 bağlantı
  agent/              Uygulama, SAP tanı araçları ve testler
  sap/                Kapalı bağlantı taslakları ve BT rehberi
  transfer/           Doğrulama, hedef hazırlığı ve test yardımcıları
  requirements.txt    Aday bağımlılık listesi
```

`app/MANIFEST.json`, yalnız **app/** içeriğini doğrular; repo kökü ve Git geçmişi onun kapsamına girmez. `.git` dizini `app/` dışında kalır. Bu yerleşim, paket doğrulayıcısının tam envanter kontrolünü gevşetmeden Git ile çalışmayı sağlar.

## Başlama

Önce [devir sırasını](docs/DEVIR.md), sonra [ayrıntılı kurulum rehberini](app/README.md) ve [Claude/Codex kurulum görevini](app/Kurulum_Gorevi.md) okuyun. Ayrıntılı uygulama rehberindeki **paket kökü** bu repoda `app/` klasörüdür.

Mevcut uv/Python kurulumu kontrol edildikten sonra repo kökünde:

```sh
cd app
export PYTHONDONTWRITEBYTECODE=1
uv run --no-project --python 3.12 python transfer/verify_package.py .
```

Bu kontrol kurulum yapmaz, servis başlatmaz veya bağlantı çağırmaz. Sonuç `verified` olmalı. Hash/envanter uyuşmazlığını gizlemek için manifesti değiştirmeyin; kaynak sürüm ve değişiklikleri inceleyin.

İlk hedef **Mac mini/macOS**, 1–5 kişilik şirket içi pilotun tek operatörle kabulüdür. Kurulum adayı **Python 3.12 + Langflow 1.12.4**. Yalnız eksik araçları ve bağımlılıkları ayrı ortama kurun; sistem Python'una veya başka agentın ortamına kurulum yapmayın. İlk bağımlılık kurulumu internet gerektirir. Bütün bağımlılıkların binary'leri, çevrimdışı wheel deposu veya hedefe özgü tam lock dosyası repoda bulunmaz.

`app/README.md` izole ortamı oluşturduktan sonra, `app/` içinde üç test grubu çalıştırılır:

```sh
SATINALMA_RUNTIME="$HOME/Library/Application Support/SatinAlmaAgent/runtime"
export PYTHONDONTWRITEBYTECODE=1
env -u SATINALMA_CONFIG -u SATINALMA_STATE_DIR uv run --no-project --no-sync --python "$SATINALMA_RUNTIME/bin/python" python transfer/run_offline_tests.py --suite root
env -u SATINALMA_CONFIG -u SATINALMA_STATE_DIR uv run --no-project --no-sync --python "$SATINALMA_RUNTIME/bin/python" python transfer/run_offline_tests.py --suite components
env -u SATINALMA_CONFIG -u SATINALMA_STATE_DIR uv run --no-project --no-sync --python "$SATINALMA_RUNTIME/bin/python" python -m pytest -q -p no:cacheprovider agent/components/test_claude_supplier_search.py
```

Kaynak 0.3.0 paketinin **22 Ekim 2026** devir kontrolünde 372 temel + 416 bileşen + 18 protokol = **806 çevrimdışı test** geçti. Bu tarihli sonuç, yeni checkout'un veya hedef bilgisayarın güncel test sonucu değildir. Her kurulumda gerçek sonuçlar yeniden kaydedilir. Testler sentetik kaynaklar kullanır; gerçek SAP, posta veya ortak model koşusunu kanıtlamaz.

**29 Ekim 2026 repo hazırlığında** aynı üç grup bağımsız `app/` klasöründen yeniden çalıştırıldı ve **806 test geçti**. Kapsam ve sınırlar [doğrulama kaydında](docs/DOGRULAMA_2026-10-29.md) yer alır.

## Ayarlar ve gerçek bağlantılar

Config, state, Langflow veritabanı, log, rapor ve sırlar repo dışında tutulur. `app/config.example.json` ve `app/sap/` şablonları gerçek bağlantı değildir. API anahtarlarını/parolaları sohbete, dosyaya veya Git'e yazmayın; kullanıcı bunları hedefteki güvenli yerel giriş alanından sağlar.

| Konu | Ayrı tamamlanacak kabul |
|---|---|
| Langflow | İzole kurulum, loopback, kimlik doğrulama, yeni akış UUID'si ve sentetik çalışma. |
| OpenAI + Anthropic | Hedef API/kota/model uyumu ve aynı görevde küçük gerçek ortak koşu. |
| SAP | VPN/DNS, katı TLS, bağımsız adapter kimliği/okuması, 12 alan eşlemesi, durum/kişi/organizasyon ve hammadde-sarf kapsamı. |
| SAP hesabı | Sürekli kullanım öncesi dar kapsamlı teknik salt okunur hesap ve yetki kabulü. |
| Bot e-posta | Ayrı adres ve yalnız o kutuya erişim; kişisel Outlook'a geçiş yapılmaz. |
| İşletim | Yedekten dönüş, yeniden başlatma, uyku/VPN davranışı ve uzaktan erişim. |
| Sürekli otomasyon | Bağımsız posta okuyucu/gönderici, zamanlayıcı ve çok kullanıcılı satın alma onay paneli bu teslimde tamamlanmamıştır. |

Kaynak DEV GUI'de metadata ve seçilmiş tek kalem GET okuması doğrulanmıştır. Bu, hedef adapter kabulü değildir. Örnek kısmen siparişli olduğu için araştırmaya alınmamıştır. Ham API kalem anahtarı korunur; sıfır silme veya durum varsayımıyla kabul kapıları aşılmaz. Ayrıntılar [SAP rehberinde](app/sap/README.md) ve [BT teslim notundadır](app/sap/BT_Teslim_Notu.md).

İstenen operasyon takvimi Türkiye saatiyle günlük 10.00/16.00 talep-teklif kontrolü ve pazartesi/cuma hammadde haberleridir. Repo bu takvimi kendiliğinden kurmaz; önceki bilgisayardaki otomasyonlar taşınmaz.

## Paylaşım ve geliştirme

Repo özeldir. Özel GitHub reposunu klonlayacak kişinin GitHub hesabı, doğru kullanıcıya verilmiş erişim ve kabul edilmiş daveti gerekir. Hesabı olmayan alıcı için kaynak/kurulum ZIP'i ayrı paylaşılabilir; repo bağlantısı tek başına erişim sağlamaz. Yeni erişim daveti göndermeden önce hedef hesap ve izin düzeyi doğrulanır.

Bu devirde `app/` işlevsel kaynakları 0.3.0 paketinden aynen korunur. Sonraki geliştirmeler ayrı dalda yapılmalı; uygulama sürümü, açık dosya seçimi, hash envanteri ve temiz çıkarım testleri birlikte güncellenmelidir. Hata kontrolünü geçmek için doğrulayıcı gevşetilmez. Çalışan pilot config/state'i geliştirme verisi olarak kullanılmaz.

`app/transfer/build_package.py` bir `--files` seçim dosyası ister; başlangıç teslimi bu seçim listesini içermez. Bu repo hazır tek komutla yeni sürüm üretildiği iddiasında bulunmaz. Yeniden paketleme seçimi ve sürümleme, sonraki geliştirmede ayrıca hazırlanıp doğrulanmalıdır; `app/` içine gelişigüzel dosya eklenmez.

Langflow lisans bildirimi [app/LICENSE.langflow.txt](app/LICENSE.langflow.txt) içinde korunur. Bu repo için ayrıca yeni bir lisans verilmiş olduğu varsayılmaz; daha geniş yeniden dağıtım öncesi hak ve üçüncü taraf bildirimleri değerlendirilir.
