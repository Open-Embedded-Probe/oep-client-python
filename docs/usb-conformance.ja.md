# raw USBの適合検査

bulk/HIDのprobe入力を、公開clientの独立した検査器で確認する。通常の `usb_stream` / `hid_stream` のencoder/decoder、再送、立て直しを通さない。SPECのtransport §1/2/3とcore §2.4が合否の根拠。backendは2つ: `core-v1` のソフトウェアUSBモデル（実際のUSB deviceではない）と、実機を開く `usb_hw`（pyusb）。

## 最小モデルの実行

`tests/equipment/.env.example` から明示設定を作り、unitを `virtual-core-1`、SPEC checkout、新規JSON出力先、既存のlockファイルを指定する。モデル用入口はこの4設定だけを使う。通常入口のADDRESS/FRAMING/TCP_PEERを使わず、実機へ接続しない。隣接checkoutや私的設備台帳を探索しない。

```sh
uv run --group dev --env-file tests/equipment/.env \
  python -m oep_client.conformance.usb_model --kind bulk
# 別の新規出力先を指定して実行する
uv run --group dev --env-file tests/equipment/.env \
  python -m oep_client.conformance.usb_model --kind hid --out /absolute/path/to/new-hid.json
```

ID付きHIDの既定はreport ID 6、input 9 byte、output 11 byte。サイズはID/countを含む。`--report-id 0` ならIDを使わないので、別IDの入力だけnot_applicableとなり理由を残す。`--input-size` / `--output-size` で別のサイズも試せる。実機のdescriptorをこの値から推定しない。

| 検査 | 刺激・観測 |
|---|---|
| bulk/HID共通 | byte単位の分割、結合、length 0、短い要求、未知role、length/body途中の放棄とgap後の回復、過大lengthとgapまでの破棄、宣言max_frame丁度の要求 |
| HIDだけ | count 0、任意値の受信paddingを無視、別report IDの入力を破棄、report容量を超えるcountとgapまでの破棄、SET_REPORT（通常のOUTも全ケースで使用） |

異常countと過大lengthの直後にも正常要求を送り、gap以前の入力をすべて捨てることを確認する。途切れの観測はSPECのframe gap＋100ms。無応答、回復後の単一応答、boot/corr、raw transfer/report、method、時間を保存する。max_frame 65535の過大lengthだけはu16で表現できずnot_applicable。frame/report破損・アクセスエラーをskipや再試行で隠さない。

## 実機の実行

`usb_hw` は、利用者が名指した1台だけを開く: VID:PID（省略時はSPECのregistryのproject VID:PID）で、serial numberがunit idと同じ（大文字小文字を区別しない）device。共通lockを取ってから開き、descriptorを読み、bulkはOEPのvendor interfaceを、HIDはusage page 0xFF4F・usage 0x45のinterfaceをclaimする。shape（bulkのwMaxPacketSize、HIDのreport長とID）はdescriptorから取る。

```sh
uv run --group dev python -m oep_client.conformance.usb_hw --kind bulk \
  --unit UNIT --spec /path/to/oep-spec --out /absolute/path/to/new.json --lock /path/to/bench.lock
```

上の表の検査に加えて、実機だけで次を確かめる。

| 検査 | 刺激・観測 |
|---|---|
| USB-DESC-SERIAL / VID-PID | serial numberがunit id。VID:PIDは記録（projectのものか、名指したものか） |
| USB-DESC-VENDOR / HID | class 0xFF・subclass 0x4F・protocol 0x45のinterfaceが高々1つでbulk IN/OUTが1組。usage page 0xFF4F・usage 0x45のHIDが高々1つで、report IDが高々1つ |
| USB-DESC-WINUSB | MS OS 2.0のcompatible ID `WINUSB`（SHOULD。無ければwarning） |
| CORE-USB-IN-END（bulk） | `oep.probe.link` のsourceで、枠がちょうどIN packet 1個分と2個分の答えを出させ、続くdataなしで届くこと（probeの最後の転送の終わり、transports §1）。linkが無いか、max_frameが足りなければnot_applicable |

`--peer hid|bulk`（同じdeviceのもう一方のUSBの経路）か `--peer-port PATH`（そのOEPのCDCの口）を付けると、2つ目の経路を開き、2つの経路の検査 CORE-ROUTE-PEER-IDENTITY / LOCK / ROUTE（transports §3: 同じ個体・boot・describe、別のtransportの番号、1つのロックと持ち主、答えは要求の来た経路にだけ）も回す。

## raw adapterの契約

`oep_client.conformance.usb.inspect(factory, ...)` が、新規出力先の予約と共有lockの取得後にfactoryを呼ぶ。shapeが不正ならfactoryを呼ばない。終了・例外時にもbackendをcloseし、close失敗は実行全体をFAILにする。個体不一致・不正confirmではsession操作へ進まない。

factoryは、利用者が明示した端点を開くraw backendを返す。backendの `write(data, method='out'|'set-report')` は送ったbyte数を返し、`read(timeout)` は1転送/reportのbytes、timeout時だけNoneを返す。bulkのbytes長0はZLPであり、timeoutとは区別する。切断・権限・その他のUSBエラーは例外で伝える。ID/count/paddingを先に除去・修復したり、要求の再送やframing回復を行ったりしない。

HIDのshapeには両方向のreport長と共通ID（0はIDなし）、bulkにはOUTのwMaxPacketSizeを渡す。検査器自身がHIDのcount/IDを符号化・検査し、bulkのOUT書込みがpacketサイズの倍数ならZLPを続ける。受信側は任意のpaddingを無視する義務があり、probeは送信paddingを0にする義務がある。入力刺激では非zero paddingの受信を試し、probe出力では非zero paddingや未宣言ID/過大countを違反として報告する。hostが未知IDを読み飛ばす規則と、probeが未宣言のreportを送ってよいかは別の検査である。

抜き差し・再列挙・給電は検査しない。通常CLIの `--framing client` でUSBを使う結果はメッセージ検査のみで、この独立wire検査の証拠にはしない。

必須の `tests/test_conformance_usb.py` はliteral bytes、異常応答、欠落・重複、任意の転送境界、countの上位byte、IDなし/あり、小/大report、破損したprobeモデルで検査器を検証する。モデルの成功を実機USBの成功や全コア準拠と扱わない。
