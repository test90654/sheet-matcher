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


def match_dataframes(df_master, df_price, threshold=MATCH_THRESHOLD, review_threshold=REVIEW_THRESHOLD):
    """マスターと価格表を突合し、結果のDataFrameを返す"""
    master_name_col = next((c for c in df_master.columns if '商品名' in c), df_master.columns[1] if len(df_master.columns) > 1 else df_master.columns[0])
    price_name_col = next((c for c in df_price.columns if '商品名' in c), df_price.columns[1] if len(df_price.columns) > 1 else df_price.columns[0])
    price_col = next((c for c in df_price.columns if '価格' in c or '買取' in c), df_price.columns[-1])

    merged_df = df_master.copy()
    if price_col not in merged_df.columns:
        merged_df[price_col] = ''
    merged_df['一致方法'] = ''
    merged_df['類似度'] = ''
    merged_df['価格表側の商品名'] = ''
    merged_df['要確認_候補価格'] = ''

    master_names = merged_df[master_name_col].astype(str).str.strip()
    master_cleaned = master_names.map(clean_text)
    price_names = df_price[price_name_col].astype(str).str.strip()
    price_cleaned = price_names.map(clean_text)
    price_values = df_price[price_col]

    def is_unmatched():
        return merged_df[price_col].isna() | (merged_df[price_col] == '')

    # 1. 完全一致
    print("商品名による完全一致処理を実行中...")
    price_dict = dict(zip(price_names, price_values))
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


def write_result(sh, merged_df):
    """結果タブを削除せずに中身だけ上書きする（参照・書式・フィルタを保持）"""
    merged_df = merged_df.fillna('').astype(str)
    data_to_write = [merged_df.columns.tolist()] + merged_df.values.tolist()
    rows, cols = len(data_to_write), len(data_to_write[0])

    try:
        ws = sh.worksheet(OUTPUT_SHEET_NAME)
        print(f"既存のタブ「{OUTPUT_SHEET_NAME}」の内容を上書きしています...")
        ws.clear()
        if ws.row_count < rows or ws.col_count < cols:
            ws.resize(rows=max(ws.row_count, rows), cols=max(ws.col_count, cols))
    except gspread.exceptions.WorksheetNotFound:
        print(f"新しいタブ「{OUTPUT_SHEET_NAME}」を作成して書き込んでいます...")
        ws = sh.add_worksheet(title=OUTPUT_SHEET_NAME, rows=rows + 10, cols=cols + 5)

    ws.update(range_name='A1', values=data_to_write)


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

    merged_df = match_dataframes(df_master, df_price, threshold, review_threshold)
    write_result(master_sh, merged_df)
    print("すべての処理が完了しました！")


if __name__ == "__main__":
    if not MASTER_ID or not PRICE_ID:
        raise ValueError("スプレッドシートのIDが環境変数に設定されていません。")
    execute_sheet_matching(MASTER_ID, PRICE_ID)
