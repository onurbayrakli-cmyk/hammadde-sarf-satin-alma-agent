# Mac mini kurulum ve pilot kabul kaydı · 0.3.0

Bu dosya bir **boş hedef kabul şablonudur**. Hiçbir kutu hedefte kanıt görülmeden işaretlenmez. Bir kopyasını sürüm klasörünün dışındaki `acceptance/` dizinine kaydedin. Şifre, token, kullanıcı e-postası, gerçek malzeme/teklif içeriği veya SAP belge numarası yazmayın. Kaynak bilgisayar testlerini hedef testi olarak kaydetmeyin.

## 1. Teslim ve makine

| Alan | Hedefte doldurulacak değer |
|---|---|
| Tarih ve saat dilimi | |
| Paket adı / sürüm | Hammadde_Sarf_MacMini_v0.3.0.zip / 0.3.0 |
| ZIP SHA-256 ve ayrı teslim kaydı eşleşmesi | |
| Manifest doğrulaması / dosya sayısı | |
| macOS sürümü / Apple Silicon veya Intel | |
| Python / uv / Langflow / lfx sürümü | |
| Çözümlenen bağımlılık kaydının konumu | |
| İzole release / runtime / state ayrımı | |
| Kullanılacak boş loopback portu | |
| Başka kurulumlarla çakışma incelemesi | |

## 2. Yerel kabul

- [ ] Yeni boş klasöre çıkarım; hash ve tam manifest doğrulaması geçti.
- [ ] Mevcut kurulumlar korundu; bu uygulamanın ayrı ortamı ve kalıcı DB'si var.
- [ ] İzole ortam bağımlılık kurulumu ve `pip check` geçti; sürümler kaydedildi.
- [ ] Kök unittest, bileşen unittest, ayrı Claude protokol testi geçti; sayılar kaydedildi.
- [ ] Eksik dış config ile paket worker'ı kapalı duruyor; eski geliştirme state'ine düşmüyor.
- [ ] `preflight` çıktısı okundu; bağlantı testi sayılmadı.
- [ ] `prepare-flow` 21 düğüm / 28 bağlantı, altı yol bağı ve yeni dosya üretti.
- [ ] Langflow yalnız loopback; auto-login kapalı; anonim flow API isteği reddediliyor.
- [ ] Sağlık, hazır olma, sürüm ve oturum açma ayrı ayrı doğrulandı.
- [ ] Hazırlanan akış yeni UUID ile içe aktarıldı; config doğru UUID'yi içeriyor.
- [ ] Worker doğru akışı okuyor; boş girişlerde iş/model/posta işlemi başlamıyor.
- [ ] Ayrı sentetik state ile çıktı dosyası ve revizyonu doğrulandı.
- [ ] Sır içermeyen yerel log ve saklama alanı belirlendi.

Test sonuçları ve özel veri içermeyen kanıt konumları:

```
Kök test:
Bileşen test:
Protokol test:
Paket doğrulama:
Akış / çıktı revizyonu:
```

## 3. Ayrı bağlantı ve iş kabulü

Her satır için `bekliyor / geçti / başarısız / kapsam dışı` ve kanıt tarihi yazın.

| Kontrol | Durum ve özel veri içermeyen kanıt |
|---|---|
| OpenAI küçük seçilmiş sentetik çağrı / kota | |
| Anthropic küçük seçilmiş sentetik çağrı / arama yetkisi | |
| Aynı görevde gerçek ortak iki-model koşusu | |
| Şirket ağı/VPN ve hedef DNS/port | |
| SAP adapteriyle katı TLS ve güven zinciri | |
| SAP adapteriyle kimlik ve metadata | |
| SAP adapteriyle yalnız seçilmiş tek ham API anahtarı okuması | |
| API / SAP kaynak ekranı eşitliği | |
| Ham durumların ve 12 alan eşlemesinin iş onayı | |
| Hammadde/sarf, tesis/organizasyon/malzeme grubu kapsamı | |
| Talep sahibinin doğrulanmış eşlemesi | |
| Tamamen açık ve siparişsiz kalemin insan onaylı intake'i | |
| Süre eksik / kapsam dışı / kapalı / siparişli kalemin reddi | |
| Ayrı teknik hesap ve sunucu tarafında dar okuma yetkisi | |
| Ayrı bot kutusu ve yalnız o kutuya erişim | |
| Kaynaklı teklif, KDV, vade ve teknik karşılaştırma | |
| %1–2 indirim ve kaynaklıysa ayrı +30 gün pazarlık taslağı | |
| Süre ve teklif eşleştirme / geç teklif ayrımı | |

Kaynak bilgisayarda DEV GUI okuması doğrulanmıştır. Yukarıdaki hedef satırları bu yüzden kendiliğinden `geçti` olmaz. Bu paket; otomatik posta okuyucu/gönderici, bağımsız zamanlayıcı, çok kullanıcılı onay paneli veya sipariş/SAP yazma işlevinin kabulünü sağlamaz.

## 4. İşletim ve kurtarma

- [ ] Tek kuyruk yazarı ve işletim sorumlusu belirlendi.
- [ ] Uyku/güç ve VPN kesintisi davranışı kaydedildi.
- [ ] Manuel başlatma/durdurma doğrulandı.
- [ ] Kalıcı servis varsa ayrı olarak oturum kapatma ve Mac yeniden başlatması sınandı.
- [ ] Uzak erişim gerekiyorsa VPN/SSH tüneli, ayrı hesaplar ve erişim reddi doğrulandı.
- [ ] Tutarlı DB + rapor/araştırma + Langflow ayar yedeği alındı.
- [ ] Yedek boş geçici hedefe geri açıldı; kayıt/ek/revizyon bağları kontrol edildi.
- [ ] Yayın geri dönüşü ile iş verisi geri dönüşü ayrıldı; sonraki dış işlemler korunuyor.
- [ ] Hata halinde durdurma, kayıt koruma ve BT'ye aktarım sorumluluğu belli.

## 5. Karar

**Kurulum sonucu:** tamamlanmadı / çevrimdışı kurulum geçti / yerel sentetik pilot geçti / dar canlı pilot geçti.

**Açık engeller ve sorumluları:**

**Bir sonraki somut adım:**

**Canlı işleme izin verilen dar kapsam:**

**İnsan kontrolü ve dış ileti sınırı:** Tüm dış yazışmalar taslak; gönderim/indirim-vade kabulü/sipariş/SAP yazma bu sürümde otomatik yapılmaz.

Tamamlanmayan koşul için başarı yazmayın; kod değişikliği veya bağımlılık uyumsuzluğu varsa sürüm dosyalarını yerinde düzeltmek yerine ayrı inceleme ve yeni paket isteyin.
