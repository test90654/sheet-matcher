import os
import json
import re
import gspread
from google.oauth2.service_account import Credentials
import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process

# スプレッドシートIDの取得
MASTER_ID = os.environ.get("MASTER_SHEET_ID")
PRICE_ID = os.environ.get("PRICE_SHEET_ID")

# しきい値（環境変数で上書き可能）
MATCH_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", 95))
REVIEW_THRESHOLD = float(os.environ.get("REVIEW_THRESHOLD", 85))

OUTPUT_SHEET_NAME = '突合結果_自動出力'
# 手動指定の保存先と、プルダウンの選択肢に使う価格表の商品名リスト
MANUAL_SHEET_NAME = '手動対応表'
LIST_SHEET_NAME = '価格表_商品名リスト'
MANUAL_COL = '手動指定'
NO_MATCH = '(一致させない)'

# 追加の買取価格表（店舗ごとに価格列を横に並べる）
# (列名の接頭辞, スプレッドシートIDの環境変数名)。IDが未設定の店舗は飛ばす。
# figure-kaitori-bot の形式（「現在一覧」タブ、状態=削除 の行は対象外）を想定。
EXTRA_PRICE_SOURCES = [
    ('千葉鑑定団', 'CHIBAKAN_PRICE_ID'),
]
EXTRA_PRICE_TAB = '現在一覧'


def extra_names(label):
    """追加店舗ごとの 手動指定の列名・手動対応表のタブ名・プルダウン選択肢のタブ名"""
    return f'{label}_{MANUAL_COL}', f'{MANUAL_SHEET_NAME}_{label}', f'{LIST_SHEET_NAME}_{label}'

EXTRA_STATUS_COL = '状態'
EXTRA_REMOVED = '削除'

# タブの右側に残しておく空き列の数
SPARE_COLS = 5

# あいまい一致を一度に計算する行数（メモリ使用量の上限を抑えるため）
FUZZY_CHUNK_SIZE = 1000


def get_client():
    # GitHub Secretsの環境変数からサービスアカウントのJSON文字列を取得
    sa_key_json = os.environ.get("GCP_SA_KEY")
    if not sa_key_json:
        raise ValueError("環境変数 GCP_SA_KEY が設定されていません。")

    # プレーンテキストのJSON文字列から認証情報を直接構築
    sa_info = json.loads(sa_key_json)
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    return gspread.authorize(creds)


def clean_text(text):
    """商品名の比較精度を上げるための正規化処理"""
    if pd.isna(text) or not isinstance(text, str):
        return ""
    text = re.sub(r'[Ａ-Ｚａ-ｚ０-９]', lambda s: chr(ord(s.group(0)) - 0xFEE0), text)
    text = re.sub(r'[\s \-\ー\(\)（）\[\]【】]', '', text)
    return text.lower()


def find_columns(df_master, df_price):
    """JAN列・商品名列・価格列を列名から推定する"""
    jan_col = next((c for c in df_master.columns if 'JAN' in c or 'バーコード' in c), None)
    master_name_col = next((c for c in df_master.columns if '商品名' in c), df_master.columns[1] if len(df_master.columns) > 1 else df_master.columns[0])
    price_name_col = next((c for c in df_price.columns if '商品名' in c), df_price.columns[1] if len(df_price.columns) > 1 else df_price.columns[0])
    price_col = next((c for c in df_price.columns if '価格' in c or '買取' in c), df_price.columns[-1])
    return jan_col, master_name_col, price_name_col, price_col


def make_key(jan, name):
    """手動指定のキー：JANがあればJAN、なければ商品名"""
    jan = '' if jan is None or pd.isna(jan) else str(jan).strip()
    return jan or str(name).strip()


def match_dataframes(df_master, df_price, threshold=MATCH_THRESHOLD, review_threshold=REVIEW_THRESHOLD, manual=None):
    """マスターと価格表を突合し、結果のDataFrameを返す（manual: キー→価格表側の商品名 の手動指定）"""
    jan_col, master_name_col, price_name_col, price_col = find_columns(df_master, df_price)

    merged_df = df_master.copy()
    if price_col not in merged_df.columns:
        merged_df[price_col] = ''
    merged_df['一致方法'] = ''
    merged_df['類似度'] = ''
    merged_df['価格表側の商品名'] = ''
    merged_df['要確認_候補価格'] = ''
    keys = pd.Series([make_key(j, n) for j, n in zip(merged_df[jan_col] if jan_col else [None] * len(merged_df), merged_df[master_name_col])], index=merged_df.index)
    merged_df[MANUAL_COL] = keys.map(manual or {}).fillna('')
    # pandas 3 では '' で作った列が文字列専用型になり数値の価格を代入できないため、object型にそろえる
    merged_df = merged_df.astype(object)

    master_names = merged_df[master_name_col].astype(str).str.strip()
    master_cleaned = master_names.map(clean_text)
    price_names = df_price[price_name_col].astype(str).str.strip()
    price_cleaned = price_names.map(clean_text)
    price_values = df_price[price_col]
    price_dict = dict(zip(price_names, price_values))

    # 0. 手動指定（自動突合より優先し、以降の自動突合の対象から外す）
    manual_vals = merged_df[MANUAL_COL].astype(str).str.strip()
    locked = manual_vals != ''
    if locked.any():
        print(f"手動指定 {locked.sum()} 件を反映しています...")
        no = manual_vals == NO_MATCH
        merged_df.loc[no, '一致方法'] = '手動: 一致させない'
        found = locked & ~no & manual_vals.isin(price_dict.keys())
        merged_df.loc[found, price_col] = manual_vals[found].map(price_dict)
        merged_df.loc[found, '一致方法'] = '手動指定'
        merged_df.loc[found, '価格表側の商品名'] = manual_vals[found]
        missing = locked & ~no & ~found
        merged_df.loc[missing, '一致方法'] = '手動指定 (価格表に見つかりません)'
        if missing.any():
            print(f"  価格表に見つからない手動指定: {missing.sum()}件")

    def is_unmatched():
        return (merged_df[price_col].isna() | (merged_df[price_col] == '')) & ~locked

    # 1. 完全一致
    print("商品名による完全一致処理を実行中...")
    hit = is_unmatched() & (master_names != '') & master_names.isin(price_dict.keys())
    merged_df.loc[hit, price_col] = master_names[hit].map(price_dict)
    merged_df.loc[hit, '一致方法'] = '商品名による完全一致'
    merged_df.loc[hit, '価格表側の商品名'] = master_names[hit]

    # 2. 正規化一致
    unmatched = is_unmatched()
    if unmatched.any():
        print("表記ゆれ（スペース・全角半角）を吸収した完全一致処理を実行中...")
        valid = price_cleaned != ''
        cleaned_price_dict = dict(zip(price_cleaned[valid], zip(price_names[valid], price_values[valid])))
        hit = unmatched & (master_cleaned != '') & master_cleaned.isin(cleaned_price_dict.keys())
        found = master_cleaned[hit].map(cleaned_price_dict)
        merged_df.loc[hit, price_col] = found.map(lambda x: x[1])
        merged_df.loc[hit, '一致方法'] = '正規化による完全一致'
        merged_df.loc[hit, '価格表側の商品名'] = found.map(lambda x: x[0])

    # 3. 厳格あいまい一致（全CPUでまとめて計算）
    unmatched = is_unmatched()
    targets = merged_df.index[unmatched & (master_cleaned != '')]
    if len(targets) > 0 and len(df_price) > 0:
        print(f"残りの {len(targets)} 件について、しきい値 {threshold:g}% 以上（{review_threshold:g}% 以上は要確認）の厳格なあいまい検索を実行します...")
        choices = price_cleaned.tolist()
        best_idx = np.empty(len(targets), dtype=np.int64)
        best_score = np.empty(len(targets), dtype=np.float64)
        for start in range(0, len(targets), FUZZY_CHUNK_SIZE):
            chunk = master_cleaned[targets[start:start + FUZZY_CHUNK_SIZE]].tolist()
            scores = process.cdist(chunk, choices, scorer=fuzz.WRatio, dtype=np.float32, workers=-1)
            best_idx[start:start + len(chunk)] = scores.argmax(axis=1)
            best_score[start:start + len(chunk)] = scores.max(axis=1)
            print(f"  {min(start + FUZZY_CHUNK_SIZE, len(targets))}/{len(targets)} 件完了")

        best_score = best_score.round(1)
        cand_names = price_names.to_numpy()[best_idx]
        cand_prices = price_values.to_numpy()[best_idx]
        score_text = [f'{s:g}' for s in best_score]

        merged_df.loc[targets, '類似度'] = score_text
        merged_df.loc[targets, '一致方法'] = [f'一致なし (最高類似度: {s}%)' for s in score_text]

        ok = best_score >= threshold
        review = ~ok & (best_score >= review_threshold)

        merged_df.loc[targets[ok], price_col] = cand_prices[ok]
        merged_df.loc[targets[ok], '価格表側の商品名'] = cand_names[ok]
        merged_df.loc[targets[ok], '一致方法'] = [f'厳格あいまい一致 (類似度: {s}%)' for s in np.array(score_text)[ok]]

        merged_df.loc[targets[review], '価格表側の商品名'] = cand_names[review]
        merged_df.loc[targets[review], '要確認_候補価格'] = cand_prices[review]
        merged_df.loc[targets[review], '一致方法'] = [f'要確認 (類似度: {s}%)' for s in np.array(score_text)[review]]

        print(f"  あいまい一致: {ok.sum()}件 / 要確認: {review.sum()}件 / 一致なし: {(~ok & ~review).sum()}件")

    return merged_df


def overwrite_tab(sh, title, data_to_write):
    """タブを削除せずに中身だけ上書きする（参照・書式・フィルタを保持）。なければ作成する"""
    rows, cols = len(data_to_write), len(data_to_write[0])
    try:
        ws = sh.worksheet(title)
        print(f"既存のタブ「{title}」の内容を上書きしています...")
        ws.clear()
        # 右側に余白の列を残しておく（列を広げたり、メモ用の列を足したりできるように）
        if ws.row_count < rows or ws.col_count < cols + SPARE_COLS:
            ws.resize(rows=max(ws.row_count, rows), cols=max(ws.col_count, cols + SPARE_COLS))
    except gspread.exceptions.WorksheetNotFound:
        print(f"新しいタブ「{title}」を作成して書き込んでいます...")
        ws = sh.add_worksheet(title=title, rows=rows + 10, cols=cols + SPARE_COLS)

    ws.update(range_name='A1', values=data_to_write)
    return ws


def read_tab(sh, title):
    """タブの全セルを文字列で読む（タブがなければ空リスト）"""
    try:
        return sh.worksheet(title).get_all_values()
    except gspread.exceptions.WorksheetNotFound:
        return []


def load_manual(sh, jan_col, master_name_col, manual_sheet=MANUAL_SHEET_NAME, manual_col=MANUAL_COL):
    """手動対応表を読み、前回の結果タブの「手動指定」列での変更を反映した 手動指定の辞書を返す"""
    manual = {}
    for row in read_tab(sh, manual_sheet)[1:]:
        if len(row) >= 3 and row[0].strip() and row[2].strip():
            manual[row[0].strip()] = row[2].strip()

    # 結果タブの「手動指定」列は手動対応表の内容で毎回埋めて出力しているので、
    # 入力されていれば登録・変更、空になっていれば削除として扱う
    values = read_tab(sh, OUTPUT_SHEET_NAME)
    if values and manual_col in values[0]:
        header = values[0]
        mi = header.index(manual_col)
        ji = header.index(jan_col) if jan_col in header else None
        ni = header.index(master_name_col) if master_name_col in header else None
        if ni is not None:
            before = dict(manual)
            for row in values[1:]:
                row = row + [''] * (len(header) - len(row))
                key = make_key(row[ji] if ji is not None else None, row[ni])
                if not key:
                    continue
                v = row[mi].strip()
                if v:
                    manual[key] = v
                else:
                    manual.pop(key, None)
            added = sum(1 for k in manual if before.get(k) != manual[k])
            removed = sum(1 for k in before if k not in manual)
            print(f"結果タブの「{manual_col}」を取り込みました（登録・変更: {added}件 / 削除: {removed}件）")
    return manual


def save_manual(sh, manual, merged_df, jan_col, master_name_col, manual_sheet=MANUAL_SHEET_NAME):
    """手動指定を手動対応表タブに保存する（次回以降の実行でも引き継ぐため）"""
    names = {}
    for j, n in zip(merged_df[jan_col] if jan_col else [None] * len(merged_df), merged_df[master_name_col]):
        names.setdefault(make_key(j, n), str(n))
    data = [['キー(JANまたは商品名)', 'マスター商品名', '価格表側の商品名']]
    data += [[k, names.get(k, ''), v] for k, v in sorted(manual.items())]
    overwrite_tab(sh, manual_sheet, data)


def write_price_list(sh, df_price, price_name_col, price_col, list_sheet=LIST_SHEET_NAME):
    """プルダウンと検索サイドバーの選択肢として、価格表の商品名と価格をマスター側のタブにコピーする
    （入力規則では別のスプレッドシートの範囲を参照できないため）"""
    # 突合と同じく、同じ商品名が複数あれば最後の行の価格を使う
    prices = dict(zip(df_price[price_name_col].astype(str).str.strip(), df_price[price_col]))
    rows = [[n, '' if pd.isna(p) else str(p)] for n, p in prices.items() if n]
    overwrite_tab(sh, list_sheet, [['価格表の商品名', '買取価格'], [NO_MATCH, '']] + rows)


def write_result(sh, merged_df, dropdowns=((MANUAL_COL, LIST_SHEET_NAME),)):
    """結果タブを上書きし、各「手動指定」列に対応する価格表の商品名のプルダウンを付ける
    dropdowns: (手動指定の列名, 選択肢のタブ名) の組"""
    merged_df = merged_df.fillna('').astype(str)
    data_to_write = [merged_df.columns.tolist()] + merged_df.values.tolist()
    ws = overwrite_tab(sh, OUTPUT_SHEET_NAME, data_to_write)
    print(f"::notice::result tab written: rows={len(data_to_write)} cols={len(data_to_write[0])} sheet_cols={ws.col_count}")

    requests = []
    for col, list_sheet in dropdowns:
        if col not in merged_df.columns:
            continue
        ci = merged_df.columns.get_loc(col)
        requests.append({'setDataValidation': {
            'range': {'sheetId': ws.id, 'startRowIndex': 1, 'endRowIndex': len(data_to_write),
                      'startColumnIndex': ci, 'endColumnIndex': ci + 1},
            'rule': {
                'condition': {'type': 'ONE_OF_RANGE', 'values': [{'userEnteredValue': f"='{list_sheet}'!$A$2:$A"}]},
                'showCustomUi': True,
                # 価格表から消えた商品名が残っていても警告表示にとどめる
                'strict': False,
            },
        }})
    if requests:
        sh.batch_update({'requests': requests})


def load_extra_price_df(gc, sheet_id):
    """追加店舗の価格表を読む。「現在一覧」タブがなければ最初のタブ。掲載終了(削除)の行は除く"""
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(EXTRA_PRICE_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.get_worksheet(0)
    df = pd.DataFrame(ws.get_all_records())
    if df.empty:
        return df
    df.columns = df.columns.str.strip()
    if EXTRA_STATUS_COL in df.columns:
        df = df[df[EXTRA_STATUS_COL].astype(str).str.strip() != EXTRA_REMOVED]
    return df.reset_index(drop=True)


def add_extra_prices(merged_df, df_master, df_extra, label, threshold, review_threshold, manual=None):
    """追加店舗の価格表と突合し、「<店舗>_買取価格」などの列を結果の右側に追加する
    manual: その店舗用の手動指定（キー→その店舗の価格表の商品名）"""
    jan_col, master_name_col, _, _ = find_columns(df_master, df_extra)
    keep = [c for c in (jan_col, master_name_col) if c]
    # マスターの他の列と価格列の名前がぶつからないよう、JANと商品名だけで突合する
    sub = match_dataframes(df_master[keep].copy(), df_extra, threshold, review_threshold, manual)
    price_col = find_columns(df_master, df_extra)[3]

    merged_df[f'{label}_買取価格'] = sub[price_col].values
    merged_df[f'{label}_要確認_候補価格'] = sub['要確認_候補価格'].values
    merged_df[f'{label}_商品名'] = sub['価格表側の商品名'].values
    merged_df[f'{label}_一致方法'] = sub['一致方法'].values
    merged_df[extra_names(label)[0]] = sub[MANUAL_COL].values

    hit = (sub[price_col].astype(str).str.strip() != '').sum()
    review = (sub['要確認_候補価格'].astype(str).str.strip() != '').sum()
    print(f"[{label}] 一致: {hit}件 / 要確認: {review}件 / 価格表 {len(df_extra)}件")
    print(f"::notice::extra source {len(df_extra)} rows, matched={hit}, review={review}")
    return merged_df


def execute_sheet_matching(master_sheet_id, price_sheet_id, threshold=MATCH_THRESHOLD, review_threshold=REVIEW_THRESHOLD):
    gc = get_client()
    print("Googleスプレッドシートからデータを取得しています...")

    master_sh = gc.open_by_key(master_sheet_id)
    price_sh = gc.open_by_key(price_sheet_id)

    df_master = pd.DataFrame(master_sh.get_worksheet(0).get_all_records())
    df_price = pd.DataFrame(price_sh.get_worksheet(0).get_all_records())

    df_master.columns = df_master.columns.str.strip()
    df_price.columns = df_price.columns.str.strip()

    print(f"マスターデータ件数: {len(df_master)}件")
    print(f"買取価格表件数: {len(df_price)}件")

    jan_col, master_name_col, price_name_col, price_col = find_columns(df_master, df_price)
    manual = load_manual(master_sh, jan_col, master_name_col)

    merged_df = match_dataframes(df_master, df_price, threshold, review_threshold, manual)

    dropdowns = [(MANUAL_COL, LIST_SHEET_NAME)]
    extra_done = []
    for label, env_name in EXTRA_PRICE_SOURCES:
        sheet_id = os.environ.get(env_name, '').strip()
        if not sheet_id:
            print(f"[{label}] {env_name} が未設定のため飛ばします。")
            print(f"::warning::{env_name} is not set; skipped")
            continue
        print(f"[{label}] 買取価格表を取得しています...")
        df_extra = load_extra_price_df(gc, sheet_id)
        if df_extra.empty:
            print(f"[{label}] 価格表が空のため飛ばします。")
            print(f"::warning::{env_name}: price sheet is empty; skipped")
            continue
        manual_col, manual_sheet, list_sheet = extra_names(label)
        # 結果タブを上書きする前に、その店舗の手動指定を読んでおく
        extra_manual = load_manual(master_sh, jan_col, master_name_col, manual_sheet, manual_col)
        merged_df = add_extra_prices(merged_df, df_master, df_extra, label, threshold, review_threshold, extra_manual)
        extra_done.append((label, df_extra, extra_manual))
        dropdowns.append((manual_col, list_sheet))

    # 結果タブを上書きする前に、取り込んだ手動指定を保存しておく
    save_manual(master_sh, manual, merged_df, jan_col, master_name_col)
    write_price_list(master_sh, df_price, price_name_col, price_col)
    for label, df_extra, extra_manual in extra_done:
        _, manual_sheet, list_sheet = extra_names(label)
        _, _, extra_name_col, extra_price_col = find_columns(df_master, df_extra)
        save_manual(master_sh, extra_manual, merged_df, jan_col, master_name_col, manual_sheet)
        write_price_list(master_sh, df_extra, extra_name_col, extra_price_col, list_sheet)
    write_result(master_sh, merged_df, dropdowns)
    print("すべての処理が完了しました！")


if __name__ == "__main__":
    if not MASTER_ID or not PRICE_ID:
        raise ValueError("スプレッドシートのIDが環境変数に設定されていません。")
    execute_sheet_matching(MASTER_ID, PRICE_ID)
