# capture区画serialの周回とrelease

SPEC checkoutを明示し、`CaptureSerialChecks(SampleCaptureSerialAdapter(), spec=path).run()`を実行する。
core/transportsに加え、capture/common本文のレビュー済みSHAを照合する。private設備や実transportを使わない。
logic/analogの現在の独立モデルに実wire要求とsampleを送り、u32 serialとu64 byte positionを区別する。

5項目、4条件：logic/analogと開始serial0/0xFFFFFFFEの組合せ。
repeat、2sample/区画、2区画容量、1000Hz、max_frame256、session7。位置は2^40+17から始める。
start後に、既に解放済みの大量の過去取得を表す「保持していないprefix」のseedを明示的に置く。
これは有限の論理境界検査で、数十億回の実取得を実行したという証拠ではない。

二つの区画を取得してpauseし、次serialと各区画metadata・dataを確認する。
segmentsのinclusive from、nextからの空応答、古い/未来の番号からのoldest fallbackを実byteで照合する。
releaseはinclusive。古い番号は何も解放せず、最古を解放するとresumeし、次の区画はgapを持つ。
再送したreleaseは新たな区画を解放しない。誤ったgenerationのreleaseはcause6で取得を変えない。
未来serialのreleaseも未完成のcurrent sampleを捨てず、stopで短い区画として完成させる。
readはu64位置を返し、既に解放した位置ならgap、未取得の未来位置ならwrite_posから空を返す。
区画のposition・samples・時刻・flags・generationとlogic/analog生byteを独立に組み立てて照合する。

保持するのは最大2区画で、次serialとの差はu32の1/4未満である。任意の保持容量や保持幅全域の証明にはしない。
この共通の区画手順を他のcapture拡張へ移す際は、明示adapter・独立のsample/packing oracleを用意する。
単体25件：正常1、観測故障16、実model/Core故障4、制御不足2、本文guard2。
初期fixtureのconstructor、analog feed入口、frame容量を修正した。現在のモデル本体/Core/productionは変更していない。
mark保持、streaming/groupとの通知交錯、任意capacity、物理sampling、production Host統合は残る。
full_conformance=false。
