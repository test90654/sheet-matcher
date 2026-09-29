import os
import re
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
from rapidfuzz import fuzz, process

# 環境変数から認証情報とスプレッドシートIDを取得
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

CREDENTIALS_FILE = 'credentials.json'
creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
gc = gspread.authorize(creds)

# GitHub SecretsからIDを取得（ローカルテスト用フォールバック付き）
MASTER_ID = os.environ.get("MASTER_SHEET_ID")
PRICE_ID = os.environ.get("PRICE_SHEET_ID")

def clean_text(text):
    """商品名の比較精度を上げるための正規化処理"""
    if pd.isna(text) or not isinstance(text, str):
        return ""
    text = re.sub(r'[Ａ-Ｚａ-ｚ０-９]', lambda s: chr(ord(s.group(0)) - 0xFEE0), text)
    text = re.sub(r'[\s \-\ー\(\)（）\[\]【】]', '', text)
    return text.lower()

def execute_sheet_matching(master_sheet_id, price_sheet_id, threshold=95):
    print("Googleスプレッドシートからデータを取得しています...")
    
    master_sh = gc.open_by_key(master_sheet_id)
    price_sh = gc.open_by_key(price_sheet_id)
    
    master_ws = master_sh.get_worksheet(0)
    price_ws = price_sh.get_worksheet(0)
    
    df_master = pd.DataFrame(master_ws.get_all_records())
    df_price = pd.DataFrame(price_ws.get_all_records())
    
    df_master.columns = df_master.columns.str.strip()
    df_price.columns = df_price.columns.str.strip()
    
    print(f"マスターデータ件数: {len(df_master)}件")
    print(f"買取価格表件数: {len(df_price)}件")
    
    jan_col = next((c for c in df_master.columns if 'JAN' in c or 'バーコード' in c), df_master.columns[0])
    master_name_col = next((c for c in df_master.columns if '商品名' in c), df_master.columns[1] if len(df_master.columns) > 1 else df_master.columns[0])
    
    price_name_col = next((c for c in df_price.columns if '商品名' in c), df_price.columns[1] if len(df_price.columns) > 1 else df_price.columns[0])
    price_col = next((c for c in df_price.columns if '価格' in c or '買取' in c), df_price.columns[-1])
    
    merged_df = df_master.copy()
    if price_col not in merged_df.columns:
        merged_df[price_col] = ''
    merged_df['一致方法'] = ''
    merged_df['価格表側の商品名'] = ''
    
    # 1. 完全一致
    print("商品名による完全一致処理を実行中...")
    price_dict = dict(zip(df_price[price_name_col].astype(str).str.strip(), df_price[price_col]))
    
    for idx in merged_df.index:
        m_name = str(merged_df.loc[idx, master_name_col]).strip()
        if m_name in price_dict and m_name != '':
            merged_df.loc[idx, price_col] = price_dict[m_name]
            merged_df.loc[idx, '一致方法'] = '商品名による完全一致'
            merged_df.loc[idx, '価格表側の商品名'] = m_name

    # 2. 正規化一致
    unmatched_mask = (merged_df[price_col].isna()) | (merged_df[price_col] == '')
    if unmatched_mask.sum() > 0:
        print("表記ゆれ（スペース・全角半角）を吸収した完全一致処理を実行中...")
        cleaned_price_dict = {}
        for _, row in df_price.iterrows():
            orig_name = str(row[price_name_col]).strip()
            c_name = clean_text(orig_name)
            if c_name:
                cleaned_price_dict[c_name] = (orig_name, row[price_col])
                
        for idx in merged_df[unmatched_mask].index:
            m_name = str(merged_df.loc[idx, master_name_col]).strip()
            c_m_name = clean_text(m_name)
            if c_m_name in cleaned_price_dict and c_m_name != '':
                orig_name, price_val = cleaned_price_dict[c_m_name]
                merged_df.loc[idx, price_col] = price_val
                merged_df.loc[idx, '一致方法'] = '正規化による完全一致'
                merged_df.loc[idx, '価格表側の商品名'] = orig_name

    # 3. 厳格あいまい一致
    unmatched_mask = (merged_df[price_col].isna()) | (merged_df[price_col] == '')
    if unmatched_mask.sum() > 0:
        print(f"残りの {unmatched_mask.sum()} 件について、しきい値 {threshold}% 以上の厳格なあいまい検索を実行します...")
        price_items = []
        for _, row in df_price.iterrows():
            orig_name = str(row[price_name_col]).strip()
            price_items.append((orig_name, clean_text(orig_name), row[price_col]))
            
        cleaned_price_names = [item[1] for item in price_items]
        
        for idx in merged_df[unmatched_mask].index:
            m_name = str(merged_df.loc[idx, master_name_col]).strip()
            c_m_name = clean_text(m_name)
            if c_m_name == '' or pd.isna(c_m_name):
                continue
                
            best_match = process.extractOne(c_m_name, cleaned_price_names, scorer=fuzz.WRatio)
            if best_match:
                matched_cleaned_name, score, match_idx = best_match
                if score >= threshold:
                    orig_name, _, price_val = price_items[match_idx]
                    merged_df.loc[idx, price_col] = price_val
                    merged_df.loc[idx, '一致方法'] = f'厳格あいまい一致 (類似度: {score}%)'
                    merged_df.loc[idx, '価格表側の商品名'] = orig_name
                else:
                    merged_df.loc[idx, '一致方法'] = f'一致なし (最高類似度: {score}%)'
            else:
                merged_df.loc[idx, '一致方法'] = '一致なし'

    output_sheet_name = '突合結果_自動出力'
    print(f"マスター側のスプレッドシートに新しいタブ「{output_sheet_name}」を作成して書き込んでいます...")
    
    try:
        existing_ws = master_sh.worksheet(output_sheet_name)
        master_sh.del_worksheet(existing_ws)
    except gspread.exceptions.WorksheetNotFound:
        pass
        
    result_ws = master_sh.add_worksheet(title=output_sheet_name, rows=len(merged_df)+10, cols=len(merged_df.columns)+5)
    merged_df = merged_df.fillna('').astype(str)
    
    data_to_write = [merged_df.columns.tolist()] + merged_df.values.tolist()
    result_ws.update(data_to_write)
    print("すべての処理が完了しました！")

if __name__ == "__main__":
    if not MASTER_ID or not PRICE_ID:
        raise ValueError("スプレッドシートのIDが環境変数に設定されていません。")
    execute_sheet_matching(MASTER_ID, PRICE_ID, threshold=95)