# brain-sync → Cairn LaunchAgent 移行手順（M3, DESIGN.md D11 / §9）

旧 brain-sync の LaunchAgent 4 本を止め、`com.masato.cairn.*` 2 本に置き換える
手順。**unload・ファイル削除は不可逆操作**なので、必ず新エージェントの動作を
確認してから旧を止めること（順序が本質）。

## 0. 前提

- M3 のコード（`cairn sync all` が 90 Auto 一覧まで出力する状態）がデプロイ済み
- Vault は `~/Obsidian`（D9）。旧 `~/Documents/Obsidian Vault` は使わない
- Keychain に `brain-sync-karakeep` / `brain-sync-zotero` がある（D8、変更なし）

## 1. 新エージェントの導入と確認

```bash
cd ops/launchd
KARAKEEP_URL=https://keep.kawanode.com ZOTERO_USER_ID=9248949 ./install.sh
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.masato.cairn.sync.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.masato.cairn.weekly.plist

# 直後に 1 回走る（RunAtLoad）。ログと 90 Auto の更新時刻を確認:
tail -20 ~/Library/Logs/cairn/sync.log
ls -laT ~/Obsidian/External\ Brain/90\ Auto/
```

4 ファイル（karakeep-to-review / cairn-recent / zotero-recent /
obsidian-context）の generated 時刻が更新されていれば OK。

注: `com.masato.cairn.weekly` は M4 まで exit 1（未実装メッセージ）を出す。
weekly-error.log にその 1 行が出るのは想定内。

## 2. 旧エージェント 4 本の unload と削除

新側の動作確認後に実行:

```bash
for name in cairn karakeep zotero weekly-review; do
  launchctl bootout "gui/$(id -u)/com.masato.brain-sync.$name" 2>/dev/null || true
  rm -v ~/Library/LaunchAgents/com.masato.brain-sync.$name.plist
done
launchctl list | grep brain-sync   # 何も出なければ完了
```

## 3. 後片付け（任意・別途判断）

- `~/workspace/brain-sync/`（旧スクリプト実体 + config.env）: 参照しなくなる。
  config.env に秘密は無い（キーは Keychain）が、退避してから削除を推奨
- リポジトリ内 `legacy/brain-sync/` は M3 完了コミットで削除（履歴は git に残る）
- 旧 Vault `~/Documents/Obsidian Vault` の退避/削除はユーザー判断

## ロールバック

新エージェントに問題が出た場合: `launchctl bootout gui/$(id -u)/com.masato.cairn.sync`
で止める。旧 plist を削除前なら再 bootstrap で戻せる（旧スクリプトは
`~/workspace/brain-sync/` に残っている限り動く。ただし FDA 解除後は
旧 Vault パスへの書き込みに失敗するため、config.env の OBSIDIAN_VAULT を
`~/Obsidian` に直す必要がある）。
