# -*- coding: utf-8 -*-
import streamlit as st
import pandas as pd
import json
import os
import re
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ── 상수 ──────────────────────────────────────────────────────────────────────
_BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
DB_FILE            = os.path.join(_BASE_DIR, 'intern_data.json')
KEY_FILE           = os.path.join(_BASE_DIR, 'service_account.json')

SHEET_ID           = '1A42lr9zatORmp_eDDatIxh5f423gz9k_Ui0sJjOswZU'
SHEET_TAB_NAME     = '2026년 입사 인턴 스케쥴'
PASSWD_SHEET_NAME  = '비밀번호'
HISTORY_SHEET_NAME = '교환이력'
MARKET_SHEET_NAME  = '장터'
VACATION_SHEET_NAME = '2026년 입사 인턴 스케쥴_휴가'

ESSENTIAL_DEPTS  = {'IM', 'GS', 'OB', 'PE', '진로탐색'}
LOCATIONS        = {'일산', '구미', '강남'}
DEFAULT_LOCATION = '분당'
LOCKED_TURNS     = {'1턴', '2턴'}   # 교환 불가 턴

# ── 휴가 관련 상수 ─────────────────────────────────────────────────────────────
VACATION_PERIOD_1  = {'4턴', '5턴', '6턴', '7턴'}   # 1차 휴가 기간
VACATION_PERIOD_2  = {'8턴', '9턴', '10턴', '11턴'} # 2차 휴가 기간
VACATION_TYPES     = ['A1','A2','A3','A4','B1','B2','B3','B4','C1','C2','C3','C4']
# A = IM 과 휴가 / B = EMC 과 휴가 / C = IM·EMC 외 분당 과 또는 파견병원 휴가
BUNDANG_MIN_TURNS  = 7   # 분당 근무 최소 턴 수 (전체 13개 중)

HISTORY_HEADER = ['날짜시간', '신청자', '상대방', '교환턴',
                  '신청자값', '상대방값', '결과', '비고']
MARKET_HEADER  = ['등록ID', '등록시각', '등록자', '주고싶은턴',
                  '주고싶은값', '받고싶은과', '메시지', '상태']


# ══════════════════════════════════════════════════════════════════════════════
class DataManager:
# ══════════════════════════════════════════════════════════════════════════════

    def __init__(self):
        self.scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        self.sheet_connected = False
        self.passwd_ws   = None
        self.history_ws  = None
        self.market_ws   = None
        self.passwords   = {}
        self.market_posts = []
        self.vacation_data = {}   # {이름: {'1차': {'turn':X,'type':Y}, '2차': {'turn':X,'type':Y}}}
        self.connect_google_sheet()
        self.load_db()

    # ── 구글 시트 연결 ─────────────────────────────────────────────────────────
    def connect_google_sheet(self):
        try:
            # Streamlit Cloud: st.secrets에서 키 로드
            # 로컬 개발: service_account.json 파일에서 로드
            if "gcp_service_account" in st.secrets:
                creds_dict = dict(st.secrets["gcp_service_account"])
                creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, self.scope)
            elif os.path.exists(KEY_FILE):
                creds = ServiceAccountCredentials.from_json_keyfile_name(KEY_FILE, self.scope)
            else:
                print("구글 키 없음 (st.secrets 또는 service_account.json 필요)")
                return

            self.gc = gspread.authorize(creds)
            self.sh = self.gc.open_by_key(SHEET_ID)

            # 스케줄 탭
            try:
                self.worksheet = self.sh.worksheet(SHEET_TAB_NAME)
            except gspread.WorksheetNotFound:
                self.worksheet = self.sh.get_worksheet(0)

            # 비밀번호 탭
            try:
                self.passwd_ws = self.sh.worksheet(PASSWD_SHEET_NAME)
            except gspread.WorksheetNotFound:
                self.passwd_ws = None

            # 교환이력 탭
            try:
                self.history_ws = self.sh.worksheet(HISTORY_SHEET_NAME)
                self._ensure_header(self.history_ws, HISTORY_HEADER)
            except gspread.WorksheetNotFound:
                self.history_ws = None

            # 장터 탭 (없으면 생성)
            try:
                self.market_ws = self.sh.worksheet(MARKET_SHEET_NAME)
            except gspread.WorksheetNotFound:
                try:
                    self.market_ws = self.sh.add_worksheet(
                        title=MARKET_SHEET_NAME, rows=500, cols=len(MARKET_HEADER)
                    )
                except Exception:
                    self.market_ws = None
            if self.market_ws:
                self._ensure_header(self.market_ws, MARKET_HEADER)

            self.sheet_connected = True
            print("구글 시트 연결 성공")

        except Exception as e:
            self.sheet_connected = False
            print(f"구글 시트 연결 오류: {e}")

    def _ensure_header(self, ws, header):
        """시트 첫 행이 비어 있으면 헤더를 삽입한다."""
        try:
            rows = ws.get_all_values()
            if not rows or all(c == '' for c in rows[0]):
                ws.update('A1', [header])
        except Exception as e:
            print(f"헤더 초기화 실패: {e}")

    # ── 비밀번호 ───────────────────────────────────────────────────────────────
    def fetch_passwords_from_sheet(self):
        if not self.sheet_connected or self.passwd_ws is None:
            return {}
        try:
            rows = self.passwd_ws.get_all_values()
            if len(rows) < 2:
                return {}
            header   = [h.strip() for h in rows[0]]
            name_col = next((i for i, h in enumerate(header) if '이름' in h or '성명' in h), None)
            pw_col   = next((i for i, h in enumerate(header) if '비밀번호' in h or 'pw' in h.lower()), None)
            if name_col is None or pw_col is None:
                return {}
            result = {}
            for row in rows[1:]:
                if len(row) <= max(name_col, pw_col):
                    continue
                name = row[name_col].strip()
                pw   = row[pw_col].strip()
                if name:
                    result[name] = pw if pw else '1234'
            return result
        except Exception as e:
            print(f"비밀번호 로드 실패: {e}")
            return {}

    def check_password(self, name, password):
        return password == self.passwords.get(name, '1234')

    def update_password_in_sheet(self, name, new_password):
        if not self.sheet_connected or self.passwd_ws is None:
            return False, "비밀번호 시트에 접근할 수 없습니다."
        try:
            rows     = self.passwd_ws.get_all_values()
            header   = [h.strip() for h in rows[0]]
            name_col = next((i for i, h in enumerate(header) if '이름' in h or '성명' in h), None)
            pw_col   = next((i for i, h in enumerate(header) if '비밀번호' in h or 'pw' in h.lower()), None)
            if name_col is None or pw_col is None:
                return False, "비밀번호 시트 컬럼 오류"
            for row_i, row in enumerate(rows[1:], start=2):
                if len(row) > name_col and row[name_col].strip() == name:
                    self.passwd_ws.update_cell(row_i, pw_col + 1, new_password)
                    self.passwords[name] = new_password
                    return True, "비밀번호가 변경되었습니다."
            return False, "이름을 찾을 수 없습니다."
        except Exception as e:
            return False, f"비밀번호 변경 실패: {e}"

    # ── 교환이력 ───────────────────────────────────────────────────────────────
    def log_history_to_sheet(self, sender, receiver, turn,
                              val_sender, val_receiver, result, note=''):
        if not self.sheet_connected or self.history_ws is None:
            return
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.history_ws.append_row(
                [now, sender, receiver, turn,
                 val_sender or '', val_receiver or '', result, note],
                value_input_option='USER_ENTERED'
            )
        except Exception as e:
            print(f"교환이력 기록 실패: {e}")

    def fetch_history_data(self):
        """교환이력 시트 전체 행 반환 (UI에서 직접 호출)"""
        if not self.sheet_connected or self.history_ws is None:
            return []
        try:
            return self.history_ws.get_all_values()
        except Exception:
            return []

    # ── 장터 ───────────────────────────────────────────────────────────────────
    def fetch_market_posts(self):
        if not self.sheet_connected or self.market_ws is None:
            return []
        try:
            rows = self.market_ws.get_all_values()
            if len(rows) < 2:
                return []
            # 실제 시트 헤더 이름과 무관하게 MARKET_HEADER 열 순서로 매핑 (구버전 헤더명 호환)
            raw_header = [h.strip() for h in rows[0]]
            if len(raw_header) <= len(MARKET_HEADER):
                header = MARKET_HEADER[:len(raw_header)]
            else:
                header = MARKET_HEADER + raw_header[len(MARKET_HEADER):]
            posts  = []
            for row in rows[1:]:
                padded = row + [''] * max(0, len(header) - len(row))
                posts.append(dict(zip(header, padded)))
            return posts
        except Exception as e:
            print(f"장터 로드 실패: {e}")
            return []

    def add_market_post(self, name, give_turn, give_val, want_dept, message):
        """
        give_turn : 주고싶은 턴 (예: '3턴') 또는 '아무턴'
        give_val  : give_turn이 특정 턴일 때의 현재 값 (아무턴이면 '')
        want_dept : 받고싶은 턴 (예: '3턴, 5턴') 또는 '무관'
        """
        if not self.sheet_connected or self.market_ws is None:
            return False, "장터 시트에 접근할 수 없습니다."
        # 중복 등록 방지: 같은 사람이 같은 턴으로 이미 활성 게시물이 있으면 차단
        dupes = [p for p in self.market_posts
                 if p.get('등록자') == name
                 and p.get('주고싶은턴') == give_turn
                 and p.get('상태') == '활성']
        if dupes:
            turn_label = give_turn if give_turn != '아무턴' else '(아무 턴)'
            return False, f"이미 {turn_label}으로 등록된 활성 게시물이 있습니다. 기존 게시물을 먼저 취소해 주세요."
        try:
            post_id = datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]
            now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.market_ws.append_row(
                [post_id, now, name, give_turn, give_val, want_dept, message, '활성'],
                value_input_option='USER_ENTERED'
            )
            self.market_posts = self.fetch_market_posts()
            return True, "장터에 등록되었습니다! 🎉"
        except Exception as e:
            return False, f"장터 등록 실패: {e}"

    def close_market_post(self, post_id, new_status='마감'):
        if not self.sheet_connected or self.market_ws is None:
            return False, "장터 시트에 접근할 수 없습니다."
        try:
            rows       = self.market_ws.get_all_values()
            header     = [h.strip() for h in rows[0]]
            id_col     = next((i for i, h in enumerate(header) if h == '등록ID'),   None)
            status_col = next((i for i, h in enumerate(header) if h == '상태'),     None)
            if id_col is None or status_col is None:
                return False, "시트 구조 오류"
            for row_i, row in enumerate(rows[1:], start=2):
                if len(row) > id_col and row[id_col] == post_id:
                    self.market_ws.update_cell(row_i, status_col + 1, new_status)
                    self.market_posts = self.fetch_market_posts()
                    return True, "처리되었습니다."
            return False, "포스트를 찾을 수 없습니다."
        except Exception as e:
            return False, f"처리 실패: {e}"

    def auto_close_market_posts(self, name, turn):
        """교환 완료 후 해당 인턴의 특정 턴 게시물을 '완료'로 자동 전환 (아무턴 게시물은 유지)"""
        if not self.sheet_connected or self.market_ws is None:
            return
        try:
            rows       = self.market_ws.get_all_values()
            header     = [h.strip() for h in rows[0]]
            name_col   = next((i for i, h in enumerate(header) if h == '등록자'),     None)
            turn_col   = next((i for i, h in enumerate(header) if h == '주고싶은턴'), None)
            status_col = next((i for i, h in enumerate(header) if h == '상태'),       None)
            if any(c is None for c in [name_col, turn_col, status_col]):
                return
            for row_i, row in enumerate(rows[1:], start=2):
                if (len(row) > max(name_col, turn_col, status_col)
                        and row[name_col] == name
                        and row[turn_col] == turn   # '아무턴'은 일치하지 않으므로 자동 유지
                        and row[status_col] == '활성'):
                    self.market_ws.update_cell(row_i, status_col + 1, '완료')
            self.market_posts = self.fetch_market_posts()
        except Exception as e:
            print(f"자동 마감 실패: {e}")

    def get_market_compatibilities(self, viewer, post):
        """
        viewer가 장터 post에 응할 수 있는 교환 조합을 전부 반환.
        - 주고싶은턴이 '아무턴'이면 모든 턴을 후보로 검사
        - 받고싶은과가 '무관'이면 viewer 값의 과목 제한 없음
        Returns: list of {'turn', 'poster_val', 'viewer_val', 'valid', 'reasons', 'has_pending'}
        """
        poster    = post.get('등록자', '')
        give_turn = post.get('주고싶은턴', '')
        want_str  = post.get('받고싶은과', '무관')

        if not poster or poster not in self.df.index or viewer not in self.df.index:
            return []

        import re as _re
        _turn_pat = _re.compile(r'^\d+턴$')

        # 원하는 목록 파싱: 턴 이름(예: '5턴') vs 과목명(예: 'ANE') 구분
        want_raw = [d.strip() for d in want_str.split(',') if d.strip() and d.strip() != '무관'] \
                   if want_str and want_str not in ('무관', '') else []
        want_is_turns = bool(want_raw) and all(_turn_pat.match(w) for w in want_raw)
        want_turn_names = set(want_raw) if want_is_turns else set()  # 받고싶은 턴 이름
        want_dept_vals  = set(want_raw) if not want_is_turns else set()  # 받고싶은 과목값

        # 후보 턴 목록
        if give_turn == '아무턴':
            if want_is_turns:
                # 받을 턴 지정 모드: want_turn_names가 후보
                candidate_turns = [t for t in want_turn_names if t in self.df.columns]
            else:
                candidate_turns = list(self.df.columns)
        elif give_turn in self.df.columns:
            candidate_turns = [give_turn]
        else:
            return []

        results = []
        for t in candidate_turns:
            if t in LOCKED_TURNS:
                continue
            poster_val = self.df.loc[poster, t]
            viewer_val = self.df.loc[viewer, t]
            if not poster_val or not viewer_val or poster_val == viewer_val:
                continue

            # 받고싶은 과목 필터 (과목 지정 모드): poster가 받는 값(= viewer_val)이 want_dept_vals에 포함돼야 함
            if want_dept_vals:
                if viewer_val not in want_dept_vals:
                    continue

            # 교환 검증
            sa = self.df.loc[poster].copy()
            sb = self.df.loc[viewer].copy()
            sa[t] = viewer_val
            sb[t] = poster_val
            v1, m1 = self.validate_intern(poster, sa)
            v2, m2 = self.validate_intern(viewer, sb)
            valid   = v1 and v2
            reasons = []
            if not v1: reasons.append(f"{poster}: {', '.join(sorted(m1))} 누락")
            if not v2: reasons.append(f"나: {', '.join(sorted(m2))} 누락")

            has_pending = any(
                r['sender'] == viewer and r['receiver'] == poster
                and r['turn'] == t and r['status'] == 'pending'
                for r in self.requests
            )
            results.append({'turn': t, 'poster_val': poster_val, 'viewer_val': viewer_val,
                            'valid': valid, 'reasons': reasons, 'has_pending': has_pending})

        return sorted(results, key=lambda x: (not x['valid'], x['turn']))

    def cancel_request(self, req_id, requester):
        """본인이 보낸 pending 요청을 취소한다."""
        req = next((r for r in self.requests if r['id'] == req_id), None)
        if not req:
            return False, "요청을 찾을 수 없습니다."
        if req['sender'] != requester:
            return False, "본인의 요청만 취소할 수 있습니다."
        if req['status'] != 'pending':
            return False, f"이미 처리된 요청입니다 ({req['status']})."
        req['status'] = 'cancelled'
        self.save_db()
        return True, "요청이 취소되었습니다."

    # ── 제약 조건 분석 ─────────────────────────────────────────────────────────
    def get_exchange_constraints(self, name, turn):
        """
        'name'이 'turn'을 교환할 때의 필수과목 제약을 반환.
        - required_to_receive : 반드시 받아야 할 필수과목 집합
        - already_covered     : 다른 턴에서 이미 확보된 필수과목
        - is_free             : 제약이 전혀 없음
        """
        if name not in self.df.index:
            return None
        my_val = self.df.loc[name, turn]
        _, my_dept = self.parse_cell(my_val) if my_val else (None, None)

        # 해당 턴을 제외한 나머지 턴의 필수과목 집합
        dept_without_turn = set()
        for col in self.df.columns:
            if col == turn:
                continue
            val = self.df.loc[name, col]
            if val and val not in ('None', ''):
                _, dept = self.parse_cell(val)
                if dept:
                    dept_without_turn.add(dept)

        required_to_receive = ESSENTIAL_DEPTS - dept_without_turn
        return {
            'my_val':              my_val,
            'my_dept':             my_dept,
            'required_to_receive': required_to_receive,       # 반드시 받아야 할 과
            'already_covered':     ESSENTIAL_DEPTS - required_to_receive,  # 이미 확보
            'is_free':             len(required_to_receive) == 0
        }

    # ── 시뮬레이션 ─────────────────────────────────────────────────────────────
    def simulate_exchanges(self, name, turn):
        """
        'name'이 'turn'을 교환할 수 있는 모든 파트너를 분석한다.
        Returns list of dicts (valid 먼저, 그 다음 이름순 정렬)
        """
        if name not in self.df.index or turn in LOCKED_TURNS:
            return []
        my_val  = self.df.loc[name, turn]
        results = []

        for partner in self.df.index:
            if partner == name:
                continue
            partner_val = self.df.loc[partner, turn]
            if my_val == partner_val:
                continue  # 동일 값은 의미 없음

            sched_a = self.df.loc[name].copy()
            sched_b = self.df.loc[partner].copy()
            sched_a[turn] = partner_val
            sched_b[turn] = my_val

            v1, m1 = self.validate_intern(name,    sched_a)
            v2, m2 = self.validate_intern(partner, sched_b)
            valid   = v1 and v2
            reasons = []
            if not v1: reasons.append(f"나: {', '.join(sorted(m1))} 누락")
            if not v2: reasons.append(f"{partner}: {', '.join(sorted(m2))} 누락")

            has_pending = any(
                r['sender'] == name and r['receiver'] == partner
                and r['turn'] == turn and r['status'] == 'pending'
                for r in self.requests
            )
            results.append({
                'partner':     partner,
                'my_val':      my_val,
                'partner_val': partner_val,
                'valid':       valid,
                'reasons':     reasons,
                'has_pending': has_pending
            })

        return sorted(results, key=lambda x: (not x['valid'], x['partner']))

    def simulate_by_desired_dept(self, name, target_dept):
        """
        내가 받고 싶은 과목(target_dept)을 얻기 위한 모든 (파트너, 턴) 조합 탐색.
        IM(강남)/IM(일산)/IM 등 지역 무관하게 dept 기준으로 검색.
        Returns: list of {'partner','turn','my_val','partner_val','valid','reasons','has_pending'}
        """
        if name not in self.df.index:
            return []
        results = []
        for partner in self.df.index:
            if partner == name:
                continue
            for t in self.df.columns:
                if t in LOCKED_TURNS:
                    continue
                partner_val = self.df.loc[partner, t]
                my_val      = self.df.loc[name, t]
                if not partner_val or not my_val or partner_val == my_val:
                    continue
                if partner_val != target_dept:
                    continue
                # 교환 검증
                sa = self.df.loc[name].copy()
                sb = self.df.loc[partner].copy()
                sa[t] = partner_val
                sb[t] = my_val
                v1, m1 = self.validate_intern(name,    sa)
                v2, m2 = self.validate_intern(partner, sb)
                valid   = v1 and v2
                reasons = []
                if not v1: reasons.append(f"나: {', '.join(sorted(m1))} 누락")
                if not v2: reasons.append(f"{partner}: {', '.join(sorted(m2))} 누락")
                has_pending = any(
                    r['sender'] == name and r['receiver'] == partner
                    and r['turn'] == t and r['status'] == 'pending'
                    for r in self.requests
                )
                results.append({'partner': partner, 'turn': t, 'my_val': my_val,
                                'partner_val': partner_val, 'valid': valid,
                                'reasons': reasons, 'has_pending': has_pending})
        return sorted(results, key=lambda x: (not x['valid'], x['turn'], x['partner']))

    def simulate_multi_swap(self, name, only_need_multi=True, max_swaps=3):
        """
        2~max_swaps개 턴을 동시에 교환할 때 유효한 모든 조합을 탐색.
        only_need_multi=True: 단독으로는 불가하지만 조합으로만 가능한 경우만 반환.
        Returns: [{'swaps': [swap_dict, ...], 'alone': [bool, ...], 'needs_multi': bool}]
        """
        if name not in self.df.index:
            return []

        # 후보 수집: (partner, turn, my_val, partner_val)
        candidates = []
        for partner in self.df.index:
            if partner == name:
                continue
            for t in self.df.columns:
                if t in LOCKED_TURNS:
                    continue
                my_val      = self.df.loc[name, t]
                partner_val = self.df.loc[partner, t]
                if not my_val or not partner_val or my_val == partner_val:
                    continue
                candidates.append({'partner': partner, 'turn': t,
                                   'my_val': my_val, 'partner_val': partner_val})

        if not candidates:
            return []

        # 단일 교환 유효 여부 미리 계산
        single_valid = {}
        for c in candidates:
            key = (c['partner'], c['turn'])
            if key in single_valid:
                continue
            sa = self.df.loc[name].copy();         sa[c['turn']] = c['partner_val']
            sb = self.df.loc[c['partner']].copy();  sb[c['turn']] = c['my_val']
            v1, _ = self.validate_intern(name,         sa)
            v2, _ = self.validate_intern(c['partner'], sb)
            single_valid[key] = v1 and v2

        results = []
        n = len(candidates)

        # 조합 검증 함수
        def check_combo(combo_indices):
            swaps = [candidates[i] for i in combo_indices]
            # 같은 턴에 대해 두 개 이상 교환 불가
            turns_used = [s['turn'] for s in swaps]
            if len(set(turns_used)) != len(turns_used):
                return None
            # 나의 스케줄 시뮬레이션
            sa = self.df.loc[name].copy()
            for s in swaps:
                sa[s['turn']] = s['partner_val']
            v_me, _ = self.validate_intern(name, sa)
            if not v_me:
                return None
            # 각 파트너의 스케줄 시뮬레이션
            partner_changes = {}
            for s in swaps:
                partner_changes.setdefault(s['partner'], []).append(s)
            for partner, pswaps in partner_changes.items():
                sb = self.df.loc[partner].copy()
                for s in pswaps:
                    sb[s['turn']] = s['my_val']
                v_p, _ = self.validate_intern(partner, sb)
                if not v_p:
                    return None
            # alone 체크: 조합 내 모든 교환이 단독으로도 가능하면 복합 불필요 → 제외
            alone_list = [single_valid.get((s['partner'], s['turn']), False) for s in swaps]
            needs_multi = not all(alone_list)
            if only_need_multi and not needs_multi:
                return None
            return {
                'swaps': swaps,
                'alone': alone_list,
                'needs_multi': needs_multi,
            }

        # 2개 조합
        for i in range(n):
            for j in range(i + 1, n):
                result = check_combo([i, j])
                if result:
                    results.append(result)

        # 3개 조합 (max_swaps >= 3)
        if max_swaps >= 3 and n <= 200:
            for i in range(n):
                for j in range(i + 1, n):
                    if candidates[i]['turn'] == candidates[j]['turn']:
                        continue
                    for k in range(j + 1, n):
                        result = check_combo([i, j, k])
                        if result:
                            results.append(result)
                            if len(results) >= 100:  # 결과 상한
                                return results

        return results

    # ── 복합 교환 요청 (체인) ──────────────────────────────────────────────────
    def add_chain_request(self, sender, swaps, message=''):
        """
        복합 교환을 체인 요청으로 묶어서 등록.
        swaps: [{'receiver': ..., 'turn': ...}, ...]
        모든 상대방이 수락해야만 일괄 실행.
        """
        chain_id = datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]
        created_reqs = []

        for s in swaps:
            receiver = s['receiver']
            turn = s['turn']

            if turn in LOCKED_TURNS:
                return False, f"⛔ {turn}은(는) 교환 불가 턴입니다."
            if sender not in self.df.index or receiver not in self.df.index:
                return False, "명단에서 인턴을 찾을 수 없습니다."

            val_a = self.df.loc[sender, turn]
            val_b = self.df.loc[receiver, turn]
            if val_a == val_b:
                return False, f"{turn}: 이미 동일한 스케줄입니다."

            # 중복 체크
            for req in self.requests:
                if (req['sender'] == sender and req['receiver'] == receiver
                        and req['turn'] == turn and req['status'] == 'pending'):
                    return False, f"{turn} → {receiver}: 이미 대기 중인 요청이 있습니다."

            created_reqs.append({
                "id":           datetime.now().strftime("%Y%m%d%H%M%S%f")[:17] + str(len(created_reqs)),
                "chain_id":     chain_id,
                "sender":       sender,
                "receiver":     receiver,
                "turn":         turn,
                "val_sender":   val_a,
                "val_receiver": val_b,
                "status":       "pending",
                "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "message":      message
            })

        # 체인 전체에 대해 사전 검증 (동시에 모든 교환이 이뤄진다고 가정)
        sa = self.df.loc[sender].copy()
        for r in created_reqs:
            sa[r['turn']] = r['val_receiver']
        v_me, m_me = self.validate_intern(sender, sa)
        if not v_me:
            return False, f"⚠️ 복합 교환 시 필수과목 위반 (나: {', '.join(sorted(m_me))} 누락)"

        partner_changes = {}
        for r in created_reqs:
            partner_changes.setdefault(r['receiver'], []).append(r)
        for partner, reqs in partner_changes.items():
            sb = self.df.loc[partner].copy()
            for r in reqs:
                sb[r['turn']] = r['val_sender']
            v_p, m_p = self.validate_intern(partner, sb)
            if not v_p:
                return False, f"⚠️ {partner}의 필수과목 위반 ({', '.join(sorted(m_p))} 누락)"

        self.requests.extend(created_reqs)
        self.save_db()
        return True, f"✅ 복합 교환 요청 {len(created_reqs)}건이 등록되었습니다. (체인 ID: {chain_id})"

    # ── 조건 충족 보완 조합 탐색 ──────────────────────────────────────────────
    def find_completing_combos(self, sender, mandatory_exchanges, max_additional=2):
        """
        mandatory_exchanges([{target, turn}])를 반드시 포함하면서,
        거기에 1~2개의 추가 교환을 더해 조건이 충족되는 조합을 탐색.

        추가 교환 종류:
          1) sender ↔ 제3자  (기존: sender 추가 교환)
          2) 필수 상대방 ↔ 제3자  (신규: 상대방이 조건 충족을 위해 제3자와 교환)

        Returns: [{'additional': [...], 'all_swaps': [...]}]
          swap_dict 공통 필드: {partner, turn, my_val, partner_val, is_additional}
          보완 교환 추가 필드: {is_partner_comp: True, comp_owner: str}
        """
        if sender not in self.df.index:
            return []

        # 필수 교환 목록 구성
        mandatory_swaps = []
        for ex in mandatory_exchanges:
            target = ex['target']
            turn   = ex['turn']
            if turn in LOCKED_TURNS or target not in self.df.index:
                continue
            mv = self.df.loc[sender, turn]
            pv = self.df.loc[target, turn]
            if mv == pv:
                continue
            mandatory_swaps.append({
                'partner': target, 'turn': turn,
                'my_val': mv, 'partner_val': pv,
                'is_additional': False,
            })

        if not mandatory_swaps:
            return []

        mandatory_turns    = {s['turn']    for s in mandatory_swaps}
        mandatory_partners = {s['partner'] for s in mandatory_swaps}

        # ── 후보 풀 1: sender ↔ 제3자 (기존) ─────────────────────────────────
        add_candidates = []
        for partner in self.df.index:
            if partner == sender:
                continue
            for t in self.df.columns:
                if t in LOCKED_TURNS or t in mandatory_turns:
                    continue
                mv = self.df.loc[sender, t]
                pv = self.df.loc[partner, t]
                if not mv or not pv or mv == pv:
                    continue
                add_candidates.append({
                    'partner': partner, 'turn': t,
                    'my_val': mv, 'partner_val': pv,
                    'is_additional': True,
                    'is_partner_comp': False,
                })

        # ── 후보 풀 2: 필수 상대방 ↔ 제3자 (신규 보완 교환) ──────────────────
        for mpartner in mandatory_partners:
            for third in self.df.index:
                if third in (sender, mpartner):
                    continue
                for t in self.df.columns:
                    if t in LOCKED_TURNS or t in mandatory_turns:
                        continue
                    mv = self.df.loc[mpartner, t]
                    pv = self.df.loc[third, t]
                    if not mv or not pv or mv == pv:
                        continue
                    add_candidates.append({
                        'partner': third, 'turn': t,
                        'my_val': mv, 'partner_val': pv,
                        'is_additional': True,
                        'is_partner_comp': True,
                        'comp_owner': mpartner,
                    })

        results = []

        def get_person_a(s):
            return s.get('comp_owner', sender)

        def check_combo(additional_list):
            all_swaps = mandatory_swaps + additional_list

            # 각 사람이 동일 턴에 두 번 교환하는 경우 방지
            person_turn_used = set()
            for s in all_swaps:
                pa, pb, t = get_person_a(s), s['partner'], s['turn']
                if (pa, t) in person_turn_used or (pb, t) in person_turn_used:
                    return None
                person_turn_used.add((pa, t))
                person_turn_used.add((pb, t))

            # 모든 관련자의 스케줄을 원본에서 시작해 교환 적용
            schedules = {}
            for s in all_swaps:
                for p in (get_person_a(s), s['partner']):
                    if p not in schedules:
                        schedules[p] = self.df.loc[p].copy()
            for s in all_swaps:
                pa, pb, t = get_person_a(s), s['partner'], s['turn']
                va, vb = schedules[pa][t], schedules[pb][t]
                schedules[pa][t] = vb
                schedules[pb][t] = va

            # 모든 관련자 검증
            for name, sched in schedules.items():
                v, _ = self.validate_intern(name, sched)
                if not v:
                    return None

            return {'additional': additional_list, 'all_swaps': all_swaps}

        # 추가 1개
        for c in add_candidates:
            r = check_combo([c])
            if r:
                results.append(r)
                if len(results) >= 100:
                    return results

        # 추가 2개 (1개 결과가 적을 때만)
        if max_additional >= 2 and len(results) < 10:
            n = len(add_candidates)
            if n <= 200:
                for i in range(n):
                    for j in range(i + 1, n):
                        r = check_combo([add_candidates[i], add_candidates[j]])
                        if r:
                            results.append(r)
                            if len(results) >= 50:
                                return results

        return results

    def process_chain_action(self, chain_id, req_id, action, actor):
        """
        체인 요청의 개별 수락/거절 처리.
        - 거절: 체인 전체를 자동 취소
        - 수락: 해당 건만 accepted, 모든 건이 accepted면 일괄 실행
        """
        chain_reqs = [r for r in self.requests if r.get('chain_id') == chain_id]
        req = next((r for r in chain_reqs if r['id'] == req_id), None)
        if not req:
            return False, "요청을 찾을 수 없습니다."
        if req['status'] != 'pending':
            return False, f"이미 처리된 요청입니다 ({req['status']})."

        if action == 'reject':
            # 체인 전체 거절
            for r in chain_reqs:
                if r['status'] == 'pending':
                    r['status'] = 'chain_rejected'
                elif r['status'] == 'chain_accepted':
                    r['status'] = 'chain_rejected'
            self.save_db()
            sender = req['sender']
            self.log_history_to_sheet(
                sender, actor, req['turn'],
                req.get('val_sender', ''), req.get('val_receiver', ''),
                '복합거절', f'체인 {chain_id}'
            )
            return True, "복합 교환 전체가 거절되었습니다."

        if action == 'accept':
            req['status'] = 'chain_accepted'
            self.save_db()

            # 모든 건이 chain_accepted인지 확인
            chain_reqs = [r for r in self.requests if r.get('chain_id') == chain_id]
            all_accepted = all(r['status'] == 'chain_accepted' for r in chain_reqs)

            if not all_accepted:
                pending_cnt = sum(1 for r in chain_reqs if r['status'] == 'pending')
                return True, f"✅ 수락 완료! 나머지 {pending_cnt}건 대기 중입니다."

            # 모든 건 수락 → 최신 데이터 갱신 후 재검증 후 일괄 실행
            sender = chain_reqs[0]['sender']

            # ★ 수락 전 시트 최신 데이터 갱신
            fresh_df = self.fetch_data_from_sheet()
            if not fresh_df.empty:
                self.df = fresh_df

            # 재검증: 동시에 모든 교환이 이뤄질 때 유효한지
            sa = self.df.loc[sender].copy()
            for r in chain_reqs:
                val_b = self.df.loc[r['receiver'], r['turn']] if r['receiver'] in self.df.index else r['val_receiver']
                sa[r['turn']] = val_b
            v_me, m_me = self.validate_intern(sender, sa)

            partner_scheds = {}
            for r in chain_reqs:
                if r['receiver'] not in partner_scheds:
                    partner_scheds[r['receiver']] = self.df.loc[r['receiver']].copy()
                val_a = self.df.loc[sender, r['turn']] if sender in self.df.index else r['val_sender']
                partner_scheds[r['receiver']][r['turn']] = val_a

            all_valid = v_me
            fail_msgs = []
            if not v_me:
                fail_msgs.append(f"{sender}: {', '.join(sorted(m_me))} 누락")
            for partner, sched in partner_scheds.items():
                v_p, m_p = self.validate_intern(partner, sched)
                if not v_p:
                    all_valid = False
                    fail_msgs.append(f"{partner}: {', '.join(sorted(m_p))} 누락")

            if not all_valid:
                for r in chain_reqs:
                    r['status'] = 'chain_rejected'
                self.save_db()
                for r in chain_reqs:
                    self.log_history_to_sheet(
                        r['sender'], r['receiver'], r['turn'],
                        r.get('val_sender', ''), r.get('val_receiver', ''),
                        '자동거절', '복합 재검증 실패'
                    )
                return False, "⚠️ 복합 교환 재검증 실패:\n" + "\n".join(fail_msgs)

            # 일괄 실행
            sheet_ok = True
            for r in chain_reqs:
                val_a = self.df.loc[sender, r['turn']]
                val_b = self.df.loc[r['receiver'], r['turn']]
                self.df.loc[sender, r['turn']] = val_b
                self.df.loc[r['receiver'], r['turn']] = val_a
                if self.sheet_connected:
                    ok1 = self.update_sheet_cell(sender, r['turn'], val_b)
                    ok2 = self.update_sheet_cell(r['receiver'], r['turn'], val_a)
                    if not (ok1 and ok2):
                        sheet_ok = False

            if not sheet_ok:
                # rollback은 복잡하므로 경고만
                for r in chain_reqs:
                    r['status'] = 'error'
                self.save_db()
                return False, "⚠️ 구글 시트 반영 중 오류가 발생했습니다."

            for r in chain_reqs:
                r['status'] = 'accepted'
                self.log_history_to_sheet(
                    r['sender'], r['receiver'], r['turn'],
                    r.get('val_sender', ''), r.get('val_receiver', ''),
                    '복합수락', f'체인 {chain_id}'
                )
                self.auto_close_market_posts(r['sender'], r['turn'])
                self.auto_close_market_posts(r['receiver'], r['turn'])
            self.save_db()
            return True, f"🎉 복합 교환 {len(chain_reqs)}건 모두 완료!"

    # ── 스케줄 시트 읽기 ────────────────────────────────────────────────────────
    def fetch_data_from_sheet(self):
        if not self.sheet_connected:
            return pd.DataFrame()
        try:
            rows = self.worksheet.get_all_values()
            if not rows:
                return pd.DataFrame()

            header_row_idx = name_idx = None
            for row_i, row in enumerate(rows):
                for col_i, h in enumerate(row):
                    if '성명' in h or '이름' in h:
                        header_row_idx, name_idx = row_i, col_i
                        break
                if header_row_idx is not None:
                    break
            if header_row_idx is None:
                return pd.DataFrame()

            header    = rows[header_row_idx]
            turn_cols = [(i, h) for i, h in enumerate(header) if re.search(r'\d+턴', h)]

            data_start = header_row_idx + 1
            if data_start < len(rows):
                nxt = rows[data_start][name_idx].strip() if len(rows[data_start]) > name_idx else ''
                if not nxt or re.search(r'\d+\.\d+', nxt):
                    data_start += 1

            data, names = [], []
            for row in rows[data_start:]:
                if len(row) <= name_idx:
                    continue
                name = row[name_idx].strip()
                if not name:
                    continue
                names.append(name)
                row_dict = {}
                for col_i, turn_name in turn_cols:
                    val = str(row[col_i]).strip() if col_i < len(row) else ''
                    if val in ('None', ''):
                        val = None
                    if val and ('진료탐색' in val or '진토탐색' in val):
                        val = val.replace('진료탐색', '진로탐색').replace('진토탐색', '진로탐색')
                    row_dict[turn_name] = val
                data.append(row_dict)

            return pd.DataFrame(data, index=names)
        except Exception as e:
            st.error(f"데이터 읽기 실패: {e}")
            return pd.DataFrame()

    def update_sheet_cell(self, intern_name, turn_key, new_value):
        if not self.sheet_connected:
            return False
        try:
            cell     = self.worksheet.find(intern_name)
            row_idx  = cell.row
            all_rows = self.worksheet.get_all_values()
            header_row_num = turn_col_idx = None
            for r_i, row in enumerate(all_rows):
                for c_i, h in enumerate(row):
                    if '성명' in h or '이름' in h:
                        header_row_num = r_i + 1
                        break
                if header_row_num:
                    for c_i, h in enumerate(all_rows[header_row_num - 1]):
                        if h.strip() == turn_key.strip():
                            turn_col_idx = c_i + 1
                            break
                    break
            if turn_col_idx is None:
                return False
            self.worksheet.update_cell(row_idx, turn_col_idx, new_value if new_value else "")
            return True
        except Exception as e:
            st.error(f"구글 시트 업데이트 실패 ({intern_name}): {e}")
            return False

    # ── DB 로드 / 저장 ─────────────────────────────────────────────────────────
    def load_db(self):
        sheet_df          = self.fetch_data_from_sheet()
        self.passwords    = self.fetch_passwords_from_sheet()
        self.market_posts = self.fetch_market_posts()

        if not sheet_df.empty:
            self.df = sheet_df
            if os.path.exists(DB_FILE):
                try:
                    with open(DB_FILE, 'r', encoding='utf-8') as f:
                        db = json.load(f)
                        self.requests     = db.get('requests', [])
                        self.vacation_data = db.get('vacation_data', {})
                except Exception:
                    self.requests     = []
                    self.vacation_data = {}
            else:
                self.requests     = []
                self.vacation_data = {}
        else:
            if os.path.exists(DB_FILE):
                with open(DB_FILE, 'r', encoding='utf-8') as f:
                    db = json.load(f)
                    self.df            = pd.DataFrame(db.get('schedule', {}))
                    self.requests      = db.get('requests', [])
                    self.vacation_data = db.get('vacation_data', {})
            else:
                self.df            = pd.DataFrame()
                self.requests      = []
                self.vacation_data = {}
        self.save_db()

    def save_db(self):
        db = {
            'schedule':      json.loads(self.df.to_json(orient='columns')),
            'requests':      self.requests,
            'vacation_data': self.vacation_data,
        }
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(db, f, ensure_ascii=False, indent=4)

    # ── 유틸리티 ───────────────────────────────────────────────────────────────
    def parse_cell(self, cell_value):
        if cell_value is None or (isinstance(cell_value, float) and pd.isna(cell_value)):
            return None, None
        cell_str = str(cell_value)
        match    = re.search(r'\((.*?)\)', cell_str)
        if match:
            content = match.group(1)
            if content in LOCATIONS:
                return content, cell_str.replace(f'({content})', '').strip()
            return DEFAULT_LOCATION, cell_str
        return DEFAULT_LOCATION, cell_str.strip()

    def validate_intern(self, intern_name, temp_schedule=None):
        if temp_schedule is None:
            if intern_name not in self.df.index:
                return False, set()
            schedule_row = self.df.loc[intern_name]
        else:
            schedule_row = temp_schedule
        dept_set = set()
        for col in self.df.columns:
            val = schedule_row[col]
            if val and val not in ('None', ''):
                _, dept = self.parse_cell(val)
                if dept:
                    dept_set.add(dept)
        missing = ESSENTIAL_DEPTS - dept_set
        return len(missing) == 0, missing

    # ── 분당 근무 비율 검증 ────────────────────────────────────────────────────
    def count_bundang(self, name, schedule=None):
        """분당(기본지역) 배치 턴 수 반환"""
        if schedule is None:
            if name not in self.df.index:
                return 0
            schedule = self.df.loc[name]
        count = 0
        for col in self.df.columns:
            val = schedule[col]
            if val and str(val) not in ('None', '', 'nan'):
                loc, _ = self.parse_cell(val)
                if (loc or DEFAULT_LOCATION) == DEFAULT_LOCATION:
                    count += 1
        return count

    def validate_bundang(self, name, schedule=None):
        """분당 근무 ≥ BUNDANG_MIN_TURNS 여부"""
        return self.count_bundang(name, schedule) >= BUNDANG_MIN_TURNS

    # ── 휴가 관련 헬퍼 ────────────────────────────────────────────────────────
    def get_vacation_group(self, vac_type):
        """'A2' → 'A',  'B3' → 'B',  'C1' → 'C',  '단독'(구버전) → 'C'"""
        s = str(vac_type)
        if s.startswith('A'): return 'A'
        if s.startswith('B'): return 'B'
        return 'C'  # C* 또는 '단독'(하위호환) 모두 C 그룹

    def get_intern_vacation(self, name):
        """인턴의 휴가 배정 정보 반환. 없으면 {'1차': None, '2차': None}"""
        vac = self.vacation_data.get(name, {})
        return {
            '1차': vac.get('1차'),   # None 또는 {'turn': 'X턴', 'type': 'A2'}
            '2차': vac.get('2차'),
        }

    def auto_derive_vacation_turn(self, name, period, vac_group):
        """
        스케줄을 보고 휴가 배정 가능한 턴을 자동 탐지.
        vac_group: 'A' → IM 턴 / 'B' → EMC 턴 / 'C' → IM·EMC 외 분당 과 또는 파견병원 턴
        반환: (turn: str or None, dept: str or None, error_msg: str or None)
        """
        period_turns = sorted(
            VACATION_PERIOD_1 if period == '1차' else VACATION_PERIOD_2,
            key=lambda x: int(x.replace('턴', ''))
        )
        if name not in self.df.index:
            return None, None, f"{name}을(를) 스케줄에서 찾을 수 없습니다."

        target_dept = {'A': 'IM', 'B': 'EMC'}.get(vac_group)  # C → None
        candidates = []

        for t in period_turns:
            if t not in self.df.columns:
                continue
            val = self.df.loc[name, t]
            if not val or str(val) in ('None', '', 'nan'):
                continue
            loc, dept = self.parse_cell(val)
            loc = loc or DEFAULT_LOCATION

            # 진로탐색 기간은 휴가 불가
            if dept and '진로탐색' in dept:
                continue

            if vac_group == 'C':
                # IM·EMC 외 분당 과 OR 파견병원 배치 턴
                if (loc == DEFAULT_LOCATION and dept not in ('IM', 'EMC')) or loc in LOCATIONS:
                    candidates.append((t, dept))
            else:
                # A=IM, B=EMC
                if dept == target_dept:
                    candidates.append((t, dept))

        if not candidates:
            dept_label = {'A': 'IM', 'B': 'EMC', 'C': 'IM·EMC외 분당 과 또는 파견병원'}.get(vac_group, '기타')
            return None, None, (f"{name}의 {period} 기간({', '.join(period_turns)}) 중 "
                                f"{dept_label} 배치 턴이 없습니다.")

        # 후보가 여럿이면 첫 번째 선택 (관리자가 나중에 수동 조정 가능)
        turn, dept = candidates[0]

        # IM/EMC 2개 이상 규칙
        if dept in ('IM', 'EMC'):
            total_dept = sum(
                1 for col in self.df.columns
                if (lambda v: self.parse_cell(v)[1] if v and str(v) not in ('None','') else None)(
                    self.df.loc[name, col]) == dept
            )
            if total_dept < 2:
                return None, None, (f"{name}의 {dept}이 전체 {total_dept}개뿐이라 "
                                    f"휴가로 사용할 수 없습니다 (최소 2개 필요).")

        return turn, dept, None

    def set_intern_vacation(self, name, period, vac_type):
        """
        휴가 타입(vac_type)을 받아 스케줄에서 자동으로 휴가 턴을 탐지해 저장.
        period  : '1차' or '2차'
        vac_type: 'A1'~'A4' (IM) / 'B1'~'B4' (EMC) / 'C1'~'C4' (기타분당·파견)
        반환: (ok: bool, msg: str)
        """
        if vac_type not in VACATION_TYPES:
            return False, f"휴가 유형이 올바르지 않습니다: {vac_type}"

        group = self.get_vacation_group(vac_type)
        turn, dept, err = self.auto_derive_vacation_turn(name, period, group)
        if err:
            return False, err

        if name not in self.vacation_data:
            self.vacation_data[name] = {}
        self.vacation_data[name][period] = {'turn': turn, 'type': vac_type}
        self.save_db()
        return True, f"✅ {name} {period} 휴가 자동 배정: **{turn}** ({dept or '파견'} / {vac_type})"

    def clear_intern_vacation(self, name, period):
        """특정 인턴의 1차 또는 2차 휴가 배정 초기화"""
        if name in self.vacation_data and period in self.vacation_data[name]:
            del self.vacation_data[name][period]
            self.save_db()
        return True, f"{name} {period} 휴가 초기화 완료"

    def auto_assign_all_vacations(self):
        """
        모든 인턴 휴가 자동 배정 (1차·2차 각각).
        우선순위: A(IM) → B(EMC) → C(기타 분당 과 또는 파견병원)
        A1-A4 = 한 턴 내 1-4주차. 같은 턴+그룹의 인턴끼리 주차를 자동 분배.
        반환: [{'name': str, 'success': [msg,...], 'errors': [msg,...]}]
        """
        # ── 1단계: 각 인턴×기간 별 (턴, 그룹) 결정 ─────────────────────
        # slot_map[period][(turn, prefix)] = [(name, dept), ...]
        slot_map = {'1차': {}, '2차': {}}
        no_match = []  # (name, period)

        for name in self.df.index:
            for period in ('1차', '2차'):
                found = False
                for group, prefix in [('A', 'A'), ('B', 'B'), ('C', 'C')]:
                    t, dept, err = self.auto_derive_vacation_turn(name, period, group)
                    if t:
                        key = (t, prefix)
                        slot_map[period].setdefault(key, []).append((name, dept))
                        found = True
                        break
                if not found:
                    no_match.append((name, period))

        # ── 2단계: 같은 (턴, 그룹) 인턴끼리 1-4주차 순차 배정 ─────────
        results = {name: {'name': name, 'success': [], 'errors': []}
                   for name in self.df.index}

        for period, groups in slot_map.items():
            for (turn, prefix), interns in groups.items():
                for idx, (name, dept) in enumerate(interns):
                    week = (idx % 4) + 1          # 1,2,3,4 순환
                    vac_type = f"{prefix}{week}"   # A1, A2, ...

                    ok, msg = self.set_intern_vacation(name, period, vac_type)
                    if ok:
                        results[name]['success'].append(msg)
                    else:
                        results[name]['errors'].append(msg)

        # 미매칭 인턴 에러 기록
        for name, period in no_match:
            results[name]['errors'].append(
                f"❌ {period}: 적합한 휴가 턴을 찾지 못했습니다.")

        return list(results.values())

    # ── 휴가 자동 배정: 미리보기 / 해결방안 / 확정 ─────────────────────────

    def _generate_suggestion(self, name, period, group, error_msg):
        """실패 사유 분석 후 맞춤형 해결 방안 텍스트 반환."""
        period_turns = sorted(
            VACATION_PERIOD_1 if period == '1차' else VACATION_PERIOD_2,
            key=lambda x: int(x.replace('턴', ''))
        )
        if name not in self.df.index:
            return "스케줄에 등록되지 않은 인턴입니다."

        has_any, all_jinro = False, True
        avail = []
        for t in period_turns:
            if t not in self.df.columns:
                continue
            val = self.df.loc[name, t]
            if not val or str(val) in ('None', '', 'nan'):
                continue
            has_any = True
            loc, dept = self.parse_cell(val)
            loc = loc or DEFAULT_LOCATION
            if dept and '진로탐색' not in dept:
                all_jinro = False
                avail.append((t, dept, loc))

        if not has_any:
            return f"{period} 기간에 배치된 턴이 없습니다. 스케줄 확인이 필요합니다."
        if all_jinro:
            return (f"{period} 기간의 모든 턴이 진로탐색입니다. "
                    "턴 교환을 통해 해당 기간에 다른 과목을 확보해야 합니다.")
        if error_msg and '최소 2개 필요' in error_msg:
            td = 'IM' if group == 'A' else 'EMC'
            return (f"{td} 배치가 1개뿐이라 휴가로 사용 불가합니다. "
                    f"C그룹(기타 분당/파견)으로 배정하거나, "
                    f"턴 교환으로 {td} 배치를 늘릴 수 있습니다.")
        if error_msg and '배치 턴이 없습니다' in error_msg:
            tl = {'A': 'IM', 'B': 'EMC', 'C': '기타 분당/파견'}.get(group, '')
            owned = [f"{t}({d})" for t, d, _ in avail]
            if owned:
                return (f"{period}에 {tl} 턴이 없습니다. "
                        f"보유 턴: {', '.join(owned[:4])}. "
                        "턴 교환 또는 다른 그룹 수동 배정을 고려하세요.")
            return f"{period}에 {tl} 턴이 없습니다. 턴 교환을 통해 해결할 수 있습니다."
        return "수동 배정 또는 턴 교환을 통해 해결할 수 있습니다."

    def preview_vacation_assignments(self):
        """
        전체 인턴 휴가 자동 배정 미리보기 (저장 안 함).
        모든 그룹(A→B→C) 시도 결과 수집, 실패 시 사유+해결 제안 포함.
        """
        from datetime import datetime as _dt
        GROUP_LABELS = {'A': 'IM', 'B': 'EMC', 'C': 'IM·EMC외 분당/파견'}

        # ── 1단계: 각 인턴×기간 별 (턴, 그룹) 결정 ───────────────
        slot_map = {'1차': {}, '2차': {}}
        intern_asgn = {}

        for name in self.df.index:
            intern_asgn[name] = {}
            for period in ('1차', '2차'):
                found = False
                tried = []
                for group, prefix in [('A', 'A'), ('B', 'B'), ('C', 'C')]:
                    t, dept, err = self.auto_derive_vacation_turn(name, period, group)
                    if t:
                        slot_map[period].setdefault((t, prefix), []).append((name, dept))
                        intern_asgn[name][period] = {
                            'status': 'ok', 'turn': t, 'dept': dept,
                            'group': prefix, 'vac_type': None, 'week': None,
                            'tried_groups': [],
                        }
                        found = True
                        break
                    else:
                        tried.append({
                            'group': group,
                            'group_label': GROUP_LABELS[group],
                            'error': err or '',
                            'suggestion': self._generate_suggestion(name, period, group, err),
                        })
                if not found:
                    intern_asgn[name][period] = {
                        'status': 'fail', 'turn': None, 'dept': None,
                        'group': None, 'vac_type': None, 'week': None,
                        'tried_groups': tried,
                    }

        # ── 2단계: 주차 배정 ─────────────────────────────────────
        for period, groups in slot_map.items():
            for (turn, prefix), interns in groups.items():
                for idx, (name, dept) in enumerate(interns):
                    week = (idx % 4) + 1
                    vac_type = f"{prefix}{week}"
                    a = intern_asgn[name][period]
                    if a['status'] == 'ok':
                        a['vac_type'] = vac_type
                        a['week'] = week

        # ── 3단계: 결과 정리 ─────────────────────────────────────
        results = []
        ok_c = partial_c = fail_c = 0
        for name in self.df.index:
            entry = {'name': name, 'periods': intern_asgn[name]}
            results.append(entry)
            p1 = intern_asgn[name].get('1차', {}).get('status') == 'ok'
            p2 = intern_asgn[name].get('2차', {}).get('status') == 'ok'
            if p1 and p2:
                ok_c += 1
            elif p1 or p2:
                partial_c += 1
            else:
                fail_c += 1

        return {
            'timestamp': _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
            'results': results,
            'slot_map': {p: {f"{t}_{pf}": [(n, d) for n, d in v]
                             for (t, pf), v in g.items()}
                         for p, g in slot_map.items()},
            'summary': {
                'total': len(results), 'ok_count': ok_c,
                'partial_count': partial_c, 'fail_count': fail_c,
            }
        }

    def apply_vacation_preview(self, preview_data):
        """미리보기 결과 중 status='ok'인 항목만 실제 저장."""
        results = {r['name']: {'name': r['name'], 'success': [], 'errors': []}
                   for r in preview_data['results']}
        for entry in preview_data['results']:
            name = entry['name']
            for period in ('1차', '2차'):
                pd_ = entry['periods'].get(period, {})
                if pd_.get('status') != 'ok' or not pd_.get('vac_type'):
                    if pd_.get('status') == 'fail':
                        results[name]['errors'].append(
                            f"❌ {period}: 적합한 휴가 턴을 찾지 못했습니다.")
                    continue
                ok, msg = self.set_intern_vacation(name, period, pd_['vac_type'])
                if ok:
                    results[name]['success'].append(msg)
                else:
                    results[name]['errors'].append(msg)
        return list(results.values())

    def sync_vacation_sheet(self):
        """
        이미 존재하는 VACATION_SHEET_NAME 시트에서
        인턴 이름·턴 열 위치를 읽고, 휴가 배정 셀만 '과목\\n타입' 으로 덮어쓴다.
        시트의 나머지 구조(헤더·서식 등)는 건드리지 않는다.
        반환: (ok: bool, msg: str)
        """
        if not self.sheet_connected:
            return False, "구글 시트에 연결되어 있지 않습니다."
        if self.df.empty:
            return False, "스케줄 데이터가 없습니다."

        try:
            # ── 1) 대상 시트 열기 ─────────────────────────────────────────
            try:
                ws = self.sh.worksheet(VACATION_SHEET_NAME)
            except gspread.WorksheetNotFound:
                return False, f"'{VACATION_SHEET_NAME}' 시트를 찾을 수 없습니다."

            all_rows = ws.get_all_values()
            if not all_rows:
                return False, f"'{VACATION_SHEET_NAME}' 시트가 비어 있습니다."

            # ── 2) 헤더·이름열·턴열 위치 파악 ─────────────────────────────
            header_row_idx = name_col_idx = None
            for row_i, row in enumerate(all_rows):
                for col_i, h in enumerate(row):
                    if '성명' in h or '이름' in h:
                        header_row_idx, name_col_idx = row_i, col_i
                        break
                if header_row_idx is not None:
                    break
            if header_row_idx is None:
                return False, f"'{VACATION_SHEET_NAME}' 시트에서 '성명'/'이름' 헤더를 찾을 수 없습니다."

            header = all_rows[header_row_idx]
            # {턴이름: 열인덱스(0-based)}   예: {'4턴': 5, '5턴': 6, ...}
            turn_col_map = {}
            for ci, h in enumerate(header):
                if re.search(r'\d+턴', h):
                    turn_col_map[h.strip()] = ci

            # ── 3) 인턴별 휴가 배정 정보 수집 ─────────────────────────────
            vac_map = {}   # {인턴이름: {턴이름: vac_type}}
            for intern in self.df.index:
                vac = self.get_intern_vacation(intern)
                td = {}
                for period in ('1차', '2차'):
                    v = vac[period]
                    if v and v.get('turn') and v.get('type'):
                        td[v['turn']] = v['type']
                if td:
                    vac_map[intern] = td

            if not vac_map:
                return False, "배정된 휴가가 없습니다. 먼저 휴가를 배정해주세요."

            # ── 4) 인턴 이름 → 시트 행번호 매핑 ──────────────────────────
            name_row_map = {}   # {인턴이름: 시트행번호(1-based)}
            for row_i in range(header_row_idx + 1, len(all_rows)):
                row = all_rows[row_i]
                if name_col_idx < len(row):
                    nm = row[name_col_idx].strip()
                    if nm:
                        name_row_map[nm] = row_i + 1  # gspread는 1-based

            # ── 5) 업데이트할 셀 목록 수집 ───────────────────────────────
            cells_to_update = []   # [(row_1based, col_1based, value)]
            for intern, turns_dict in vac_map.items():
                sheet_row = name_row_map.get(intern)
                if not sheet_row:
                    continue
                for turn_name, vac_type in turns_dict.items():
                    ci = turn_col_map.get(turn_name)
                    if ci is None:
                        continue
                    # 현재 셀 값 읽기 (원래 과목)
                    original = ''
                    data_row = all_rows[sheet_row - 1]  # 0-based
                    if ci < len(data_row):
                        original = data_row[ci].strip()
                    top = original if original else '(미기재)'
                    new_val = f"{top}\n{vac_type}"
                    cells_to_update.append((sheet_row, ci + 1, new_val))  # 1-based

            if not cells_to_update:
                return False, "업데이트할 셀을 찾지 못했습니다 (인턴 이름이 시트와 일치하는지 확인)."

            # ── 6) 일괄 셀 업데이트 (batch) ──────────────────────────────
            # gspread batch update: 셀 객체 리스트
            cell_list = []
            for r, c, val in cells_to_update:
                cell = gspread.Cell(row=r, col=c, value=val)
                cell_list.append(cell)

            ws.update_cells(cell_list, value_input_option='USER_ENTERED')

            return True, (f"✅ '{VACATION_SHEET_NAME}' 시트 업데이트 완료 "
                          f"(휴가 셀 {len(cells_to_update)}개 반영)")

        except Exception as e:
            return False, f"시트 업데이트 실패: {e}"

    def validate_vacation_exchange(self, sender, receiver, turn):
        """
        turn 교환 시 휴가 규칙 검증.
        규칙: 둘 다 휴가턴이면 그룹 무관 교환 OK.
              한쪽만 휴가턴이면 차단.
        반환: (ok: bool, errors: list)
        """
        errors = []
        sv = self.get_intern_vacation(sender)
        rv = self.get_intern_vacation(receiver)

        for period in ('1차', '2차'):
            s_vac = sv[period]
            r_vac = rv[period]
            s_in_turn = bool(s_vac and s_vac['turn'] == turn)
            r_in_turn = bool(r_vac and r_vac['turn'] == turn)

            if s_in_turn and r_in_turn:
                # 둘 다 이 턴에 휴가 → 그룹 무관 교환 가능 (A↔B↔C↔단독 모두 OK)
                pass
            elif s_in_turn:
                errors.append(
                    f"⛔ {sender}의 {period} 휴가 턴({turn})입니다. "
                    f"상대방({receiver})도 {period} 휴가가 이 턴이어야 교환 가능합니다."
                )
            elif r_in_turn:
                errors.append(
                    f"⛔ {receiver}의 {period} 휴가 턴({turn})입니다. "
                    f"{sender}도 {period} 휴가가 이 턴이어야 교환 가능합니다."
                )

        return len(errors) == 0, errors

    # ── 복합 교환 사전 검증 (UI 표시용) ──────────────────────────────────────
    def validate_multi_exchange(self, sender, exchanges):
        """
        exchanges: [{'target': str, 'turn': str}, ...]
        모든 교환이 동시에 이뤄진다고 가정하고 합산 검증.
        반환: (valid: bool, error_msgs: list)
        """
        if not exchanges:
            return False, ["교환 항목이 없습니다."]

        # 중복 (target, turn) 체크
        seen_pairs = set()
        for ex in exchanges:
            pair = (ex['target'], ex['turn'])
            if pair in seen_pairs:
                return False, [f"중복된 교환 항목이 있습니다: {ex['turn']} - {ex['target']}"]
            seen_pairs.add(pair)

        temp = {}   # name -> schedule copy
        errors = []

        for ex in exchanges:
            target = ex['target']
            turn   = ex['turn']

            if turn in LOCKED_TURNS:
                errors.append(f"⛔ {turn}은(는) 교환 불가 턴입니다.")
                continue
            if sender not in self.df.index or target not in self.df.index:
                errors.append("명단에서 인턴을 찾을 수 없습니다.")
                continue

            if sender not in temp:
                temp[sender] = self.df.loc[sender].copy()
            if target not in temp:
                temp[target] = self.df.loc[target].copy()

            u_val = temp[sender][turn]
            t_val = temp[target][turn]
            if u_val == t_val:
                errors.append(f"{turn}: 나와 **{target}**의 스케줄이 동일합니다.")
                continue
            temp[sender][turn] = t_val
            temp[target][turn] = u_val

        if errors:
            return False, errors
        if not temp:
            return False, ["교환할 항목이 없습니다."]

        for name, sched in temp.items():
            valid, missing = self.validate_intern(name, sched)
            if not valid:
                errors.append(f"• {name}: {', '.join(sorted(missing))} 누락")

        # ── 분당 근무 비율 검증 ────────────────────────────────────────────────
        for name, sched in temp.items():
            bc = self.count_bundang(name, sched)
            if bc < BUNDANG_MIN_TURNS:
                errors.append(
                    f"• {name}: 분당 근무 {bc}개 (최소 {BUNDANG_MIN_TURNS}개 필요)"
                )

        # ── 휴가 규칙 검증 ────────────────────────────────────────────────────
        checked_pairs = set()
        for ex in exchanges:
            pair = tuple(sorted([sender, ex['target']])) + (ex['turn'],)
            if pair in checked_pairs:
                continue
            checked_pairs.add(pair)
            ok_v, v_errs = self.validate_vacation_exchange(sender, ex['target'], ex['turn'])
            if not ok_v:
                errors.extend(v_errs)

        return len(errors) == 0, errors

    # ── 교환 요청 (신청 시점 검증) ────────────────────────────────────────────
    def add_request(self, sender, receiver, turn, message=''):
        if turn in LOCKED_TURNS:
            return False, f"⛔ {turn}은(는) 교환이 불가능한 턴입니다."

        for req in self.requests:
            if (req['sender'] == sender and req['receiver'] == receiver
                    and req['turn'] == turn and req['status'] == 'pending'):
                return False, "이미 대기 중인 동일 요청이 있습니다."

        if sender not in self.df.index or receiver not in self.df.index:
            return False, "명단에서 인턴을 찾을 수 없습니다."

        val_a = self.df.loc[sender, turn]
        val_b = self.df.loc[receiver, turn]
        if val_a == val_b:
            return False, "두 사람의 해당 턴 스케줄이 이미 동일합니다."

        # 신청 시점 규칙 검증
        sched_a = self.df.loc[sender].copy()
        sched_b = self.df.loc[receiver].copy()
        sched_a[turn] = val_b
        sched_b[turn] = val_a
        v1, m1 = self.validate_intern(sender,   sched_a)
        v2, m2 = self.validate_intern(receiver, sched_b)
        b1 = self.validate_bundang(sender,   sched_a)
        b2 = self.validate_bundang(receiver, sched_b)
        ok_vac, vac_errs = self.validate_vacation_exchange(sender, receiver, turn)

        if not v1 or not v2 or not b1 or not b2 or not ok_vac:
            lines = ["⚠️ 규칙 위반으로 신청 불가:"]
            if not v1: lines.append(f"• {sender}: 필수과목 {', '.join(sorted(m1))} 누락")
            if not v2: lines.append(f"• {receiver}: 필수과목 {', '.join(sorted(m2))} 누락")
            if not b1: lines.append(f"• {sender}: 분당 근무 {self.count_bundang(sender, sched_a)}개 (최소 {BUNDANG_MIN_TURNS}개 필요)")
            if not b2: lines.append(f"• {receiver}: 분당 근무 {self.count_bundang(receiver, sched_b)}개 (최소 {BUNDANG_MIN_TURNS}개 필요)")
            lines.extend(vac_errs)
            return False, "\n".join(lines)

        new_req = {
            "id":           datetime.now().strftime("%Y%m%d%H%M%S%f")[:17],
            "sender":       sender,
            "receiver":     receiver,
            "turn":         turn,
            "val_sender":   val_a,
            "val_receiver": val_b,
            "status":       "pending",
            "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "message":      message
        }
        self.requests.append(new_req)
        self.save_db()
        return True, f"✅ 교환 요청 완료 ({val_a} ↔ {val_b})"

    # ── 요청 처리 (수락 / 거절) ───────────────────────────────────────────────
    def process_request(self, req_id, action):
        req = next((r for r in self.requests if r['id'] == req_id), None)
        if not req:
            return False, "요청을 찾을 수 없습니다."

        p1, p2, turn = req['sender'], req['receiver'], req['turn']
        val_a = self.df.loc[p1, turn] if p1 in self.df.index else req.get('val_sender', '')
        val_b = self.df.loc[p2, turn] if p2 in self.df.index else req.get('val_receiver', '')

        # ── 거절 ──
        if action == 'reject':
            req['status'] = 'rejected'
            self.save_db()
            self.log_history_to_sheet(p1, p2, turn, val_a, val_b, '거절됨')
            return True, "요청을 거절했습니다."

        # ── 수락 ──
        if action == 'accept':
            if p1 not in self.df.index or p2 not in self.df.index:
                return False, "명단에서 인턴을 찾을 수 없습니다."

            # ★ 수락 전 시트 최신 데이터 갱신
            fresh_df = self.fetch_data_from_sheet()
            if not fresh_df.empty:
                self.df = fresh_df
            # 최신값 재로드
            val_a = self.df.loc[p1, turn] if p1 in self.df.index else req.get('val_sender', '')
            val_b = self.df.loc[p2, turn] if p2 in self.df.index else req.get('val_receiver', '')

            # ★ 수락 시점 재검증 (그 사이 다른 교환으로 값이 바뀌었을 수 있음)
            sched_a = self.df.loc[p1].copy()
            sched_b = self.df.loc[p2].copy()
            sched_a[turn] = val_b
            sched_b[turn] = val_a
            v1, m1 = self.validate_intern(p1, sched_a)
            v2, m2 = self.validate_intern(p2, sched_b)

            if not v1 or not v2:
                lines = ["⚠️ 수락 시점 재검증 실패",
                         "(신청 이후 다른 교환으로 스케줄이 변경됐습니다)"]
                if not v1: lines.append(f"• {p1}: {', '.join(sorted(m1))} 누락")
                if not v2: lines.append(f"• {p2}: {', '.join(sorted(m2))} 누락")
                req['status'] = 'rejected'
                self.save_db()
                self.log_history_to_sheet(p1, p2, turn, val_a, val_b,
                                          '자동거절', '수락 시점 재검증 실패')
                return False, "\n".join(lines)

            # 교환 실행
            self.df.loc[p1, turn] = val_b
            self.df.loc[p2, turn] = val_a

            sheet_ok = True
            if self.sheet_connected:
                with st.spinner('구글 시트에 반영 중입니다...'):
                    ok1 = self.update_sheet_cell(p1, turn, val_b)
                    ok2 = self.update_sheet_cell(p2, turn, val_a)
                    sheet_ok = ok1 and ok2

            if not sheet_ok:
                self.df.loc[p1, turn] = val_a
                self.df.loc[p2, turn] = val_b
                return False, "구글 시트 반영 오류. 취소됩니다."

            req['status'] = 'accepted'
            self.save_db()
            self.log_history_to_sheet(p1, p2, turn, val_a, val_b, '수락됨')
            # 장터 자동 완료 처리
            self.auto_close_market_posts(p1, turn)
            self.auto_close_market_posts(p2, turn)
            return True, f"✅ 교환 완료! ({val_a} ↔ {val_b})"

        return False, "알 수 없는 동작입니다."


# ══════════════════════════════════════════════════════════════════════════════
#  Streamlit UI
# ══════════════════════════════════════════════════════════════════════════════
st.set_page_config(layout="wide", page_title="인턴 턴표 교환 시스템",
                   initial_sidebar_state="auto")

# ── 모드별 CSS ────────────────────────────────────────────────────────────────
_PC_CSS = """
<style>
/* 사이드바 너비 */
section[data-testid="stSidebar"] { width: 360px !important; }
section[data-testid="stSidebar"] > div:first-child { width: 360px !important; }

/* 메인 컨텐츠 영역 */
.block-container {
    padding-top: 2.5rem !important;
    padding-left: 2.5rem !important;
    padding-right: 2.5rem !important;
    max-width: 1100px !important;
}

/* 버튼 */
.stButton > button { min-height: 2.2rem; font-size: 1rem; }

/* 선택박스·입력 레이블 */
.stSelectbox label, .stTextInput label { font-size: 1rem !important; }

/* 표 */
.stDataFrame { font-size: 0.9rem; }

/* 제목 */
h1 { font-size: 2rem !important; }
h2 { font-size: 1.5rem !important; }
h3 { font-size: 1.2rem !important; }

/* 컬럼 간격 */
[data-testid="column"] { padding: 0 6px !important; }
</style>
"""

_MOBILE_CSS = """
<style>
.block-container {
    padding-left: 0.75rem !important;
    padding-right: 0.75rem !important;
    padding-top: 3.5rem !important;
    max-width: 100% !important;
}
[data-testid="column"] { padding: 0 2px !important; }
.stButton > button { min-height: 2.5rem; font-size: 0.9rem; }
.stSelectbox label, .stTextInput label { font-size: 0.85rem !important; }
.stDataFrame { font-size: 0.75rem; }
h1 { font-size: 1.5rem !important; }
h2 { font-size: 1.2rem !important; }
h3 { font-size: 1.1rem !important; }
</style>
"""

if 'layout_mode' not in st.session_state:
    st.session_state.layout_mode = 'mobile'   # 기본값: 모바일

# 선택된 모드에 따라 CSS 적용
st.markdown(
    _MOBILE_CSS if st.session_state.layout_mode == 'mobile' else _PC_CSS,
    unsafe_allow_html=True
)

if 'manager' not in st.session_state:
    st.session_state.manager = DataManager()
mgr = st.session_state.manager

if 'quick_confirm' not in st.session_state:
    st.session_state.quick_confirm = None
if 'multi_confirm' not in st.session_state:
    st.session_state.multi_confirm = None
if 'exchange_items' not in st.session_state:
    st.session_state.exchange_items = []
    st.session_state.ei_counter = 0
if 'combo_suggestions' not in st.session_state:
    st.session_state.combo_suggestions = None
if 'combo_page' not in st.session_state:
    st.session_state.combo_page = 0
if 'mkt_combos' not in st.session_state:
    st.session_state.mkt_combos = {}   # key: "postid_turn" -> list of combos
if 'mkt_form_version' not in st.session_state:
    st.session_state.mkt_form_version = 0
if 'mkt_post_success' not in st.session_state:
    st.session_state.mkt_post_success = ''
if 'vacation_preview' not in st.session_state:
    st.session_state.vacation_preview = None

# ── 로그인 ────────────────────────────────────────────────────────────────────
if 'user_id' not in st.session_state:
    # 모드 전환 버튼 (우상단)
    _lc, _rc = st.columns([4, 1])
    with _rc:
        _is_mobile = st.session_state.layout_mode == 'mobile'
        if st.button("💻 PC 버전으로 전환" if _is_mobile else "📱 모바일 버전으로 전환",
                     use_container_width=True, key="login_mode_toggle"):
            st.session_state.layout_mode = 'pc' if _is_mobile else 'mobile'
            st.rerun()
    with _lc:
        st.title("🏥 인턴 턴표 시스템 로그인")
    if mgr.sheet_connected:
        st.success("🟢 구글 시트와 정상적으로 연결되었습니다.")
    else:
        st.error("🔴 구글 시트 연결 실패: service_account.json 파일을 확인하세요.")
        if mgr.df.empty:
            st.warning("데이터를 불러오지 못해 로그인할 수 없습니다.")
            st.stop()
    col1, _ = st.columns([1, 2])
    with col1:
        input_id = st.text_input("이름 (예: 이규)", placeholder="이름 입력")
        input_pw = st.text_input("비밀번호 (기본: 1234)", type="password")
        if st.button("로그인", type="primary"):
            input_id = input_id.strip()
            if input_id == 'ADMIN':
                admin_pw = mgr.passwords.get('ADMIN', '1234')
                if input_pw == admin_pw:
                    st.session_state.user_id = 'ADMIN'
                    st.rerun()
                else:
                    st.error("관리자 비밀번호가 틀렸습니다.")
            elif input_id in mgr.df.index:
                if mgr.check_password(input_id, input_pw):
                    st.session_state.user_id = input_id
                    if input_pw == '1234':
                        st.session_state.force_pw_change = True
                    st.rerun()
                else:
                    st.error("비밀번호가 틀렸습니다.")
            else:
                st.error("명단에 없는 이름입니다.")
    st.stop()

user = st.session_state.user_id

# ══════════════════════════════════════════════════════════════════════════════
#  관리자 대시보드
# ══════════════════════════════════════════════════════════════════════════════
if user == 'ADMIN':
    with st.sidebar:
        st.markdown("### 🔐 관리자 모드")
        st.caption("ADMIN 계정으로 로그인됨")
        st.divider()
        if st.button("🚪 로그아웃", use_container_width=True):
            del st.session_state['user_id']
            st.rerun()
        if st.button("🔄 데이터 새로고침", use_container_width=True):
            st.session_state.manager = DataManager()
            st.rerun()

    st.title("🔐 관리자 대시보드")
    st.caption(f"인턴 수: **{len(mgr.df)}명** | 턴 수: **{len(mgr.df.columns)}개**")

    adm_tab1, adm_tab2, adm_tab3, adm_tab4, adm_tab5, adm_tab6 = st.tabs([
        "📊 스케줄 통계", "📋 전체 스케줄", "🔄 교환 이력", "📝 장터 현황", "🔑 비밀번호 관리", "📅 휴가 배정"
    ])

    # ── 관리자 탭1: 스케줄 통계 ────────────────────────────────────────────────
    with adm_tab1:
        if mgr.df.empty:
            st.warning("스케줄 데이터가 없습니다.")
        else:
            # ── 데이터 전처리: 과목·지역 분리 집계 ────────────────────────────
            ALL_LOCS = list(LOCATIONS) + [DEFAULT_LOCATION]   # 일산,구미,강남,분당

            grouped_matrix  = {}   # {턴: {과목: 인원}}  (지역 무관 합산)
            detail_matrix   = {}   # {턴: {과목(지역): 인원}}  (지역 세분화)
            loc_matrix      = {}   # {턴: {지역: 인원}}

            for col in mgr.df.columns:
                g, d, lm = {}, {}, {}
                for val in mgr.df[col]:
                    if not val or str(val) in ('None', '', 'nan'):
                        continue
                    loc, dept = mgr.parse_cell(val)
                    if not dept:
                        continue
                    loc = loc or DEFAULT_LOCATION
                    # 과목 통합 (지역 무관)
                    g[dept] = g.get(dept, 0) + 1
                    # 과목+지역 세분화 (분당은 표시 없음으로 그대로)
                    detail_key = f"{dept}({loc})" if loc != DEFAULT_LOCATION else dept
                    d[detail_key] = d.get(detail_key, 0) + 1
                    # 지역별
                    lm[loc] = lm.get(loc, 0) + 1
                grouped_matrix[col] = g
                detail_matrix[col]  = d
                loc_matrix[col]     = lm

            def make_sorted_df(matrix, priority_index=None):
                df_ = pd.DataFrame(matrix).fillna(0).astype(int)
                df_.index.name = '항목'
                if priority_index:
                    first = [x for x in priority_index if x in df_.index]
                    rest  = sorted([x for x in df_.index if x not in priority_index])
                    df_   = df_.loc[first + rest]
                else:
                    df_ = df_.sort_index()
                return df_

            # ── 1) 턴별 과목 배치 현황 (통합) ──────────────────────────────────
            st.subheader("📌 턴별 과목 배치 현황 (지역 통합)")
            gdf = make_sorted_df(grouped_matrix, priority_index=sorted(ESSENTIAL_DEPTS))
            st.dataframe(
                gdf.style.background_gradient(cmap='Blues', axis=None),
                use_container_width=True, height=min(80 + len(gdf) * 35, 500)
            )
            st.caption("지역(강남/일산/구미/분당) 구분 없이 동일 과목을 합산한 인원 수")

            # ── 2) 턴별 과목 배치 현황 (지역 세분화) ──────────────────────────
            st.subheader("📌 턴별 과목 배치 현황 (지역 세분화)")
            ddf = make_sorted_df(detail_matrix)
            # 필수과목 계열 먼저 (IM, IM(강남), IM(일산)... 순)
            ess_keys = sorted([k for k in ddf.index
                               if any(k == e or k.startswith(e + '(') for e in ESSENTIAL_DEPTS)])
            other_keys = sorted([k for k in ddf.index if k not in ess_keys])
            ddf = ddf.loc[ess_keys + other_keys]
            st.dataframe(
                ddf.style.background_gradient(cmap='Greens', axis=None),
                use_container_width=True, height=min(80 + len(ddf) * 35, 600)
            )
            st.caption("IM → 분당 IM / IM(강남) → 강남 IM / IM(일산) → 일산 IM / IM(구미) → 구미 IM")

            # ── 3) 턴별 지역 인원 현황 ────────────────────────────────────────
            st.subheader("📌 턴별 지역별 인원 현황")
            ldf = make_sorted_df(loc_matrix, priority_index=['분당', '강남', '일산', '구미'])
            st.dataframe(
                ldf.style.background_gradient(cmap='Oranges', axis=None),
                use_container_width=True, height=min(80 + len(ldf) * 35, 300)
            )
            st.caption("분당 = 지역 표시 없는 과목 / 나머지는 괄호 안 지역 기준")

            # ── 4) 과목별 전체 합산 ────────────────────────────────────────────
            st.subheader("📌 과목별 전체 인원 합산 (지역 통합)")
            total_by_dept = gdf.sum(axis=1).sort_values(ascending=False)
            col_b1, col_b2 = st.columns([2, 1])
            with col_b1:
                st.bar_chart(total_by_dept)
            with col_b2:
                st.dataframe(
                    total_by_dept.rename("총 배치 횟수").reset_index(),
                    use_container_width=True, hide_index=True
                )

            # 3) 필수과목 충족 현황
            st.subheader("📌 필수과목 충족 현황")
            st.caption(f"필수과목: {', '.join(sorted(ESSENTIAL_DEPTS))}")
            status_rows = []
            for intern in mgr.df.index:
                valid, missing = mgr.validate_intern(intern)
                depts_in_sched = set()
                for col in mgr.df.columns:
                    val = mgr.df.loc[intern, col]
                    if val and str(val) not in ('None', '', 'nan'):
                        _, dept = mgr.parse_cell(val)
                        if dept:
                            depts_in_sched.add(dept)
                status_rows.append({
                    '이름': intern,
                    '충족 여부': '✅ 충족' if valid else f'🚨 미달 ({len(missing)}개)',
                    '미달 과목': ', '.join(sorted(missing)) if missing else '-',
                    '보유 필수과목': ', '.join(sorted(ESSENTIAL_DEPTS & depts_in_sched)),
                })
            status_df = pd.DataFrame(status_rows)
            n_ok  = status_df['충족 여부'].str.startswith('✅').sum()
            n_bad = len(status_df) - n_ok
            c1, c2, c3 = st.columns(3)
            c1.metric("전체 인원", len(status_df))
            c2.metric("조건 충족", n_ok, delta=f"+{n_ok}")
            c3.metric("조건 미달", n_bad, delta=f"-{n_bad}" if n_bad else "0")
            st.dataframe(status_df, use_container_width=True, hide_index=True)

    # ── 관리자 탭2: 전체 스케줄 ────────────────────────────────────────────────
    with adm_tab2:
        if mgr.df.empty:
            st.warning("스케줄 데이터가 없습니다.")
        else:
            st.subheader("📋 전체 인턴 스케줄")
            st.dataframe(mgr.df, use_container_width=True, height=600)

            st.subheader("🔍 인턴별 상세")
            sel_intern = st.selectbox("인턴 선택", list(mgr.df.index), key="adm_intern_sel")
            if sel_intern:
                row = mgr.df.loc[sel_intern]
                detail_rows = []
                for col in mgr.df.columns:
                    val = row[col]
                    _, dept = mgr.parse_cell(val) if val and str(val) not in ('None','') else (None, None)
                    detail_rows.append({
                        '턴': col,
                        '배치값': val or '-',
                        '과목': dept or '-',
                        '필수과목 여부': '⭐' if dept in ESSENTIAL_DEPTS else ''
                    })
                v, miss = mgr.validate_intern(sel_intern)
                if v:
                    st.success(f"✅ {sel_intern}: 필수과목 모두 충족")
                else:
                    st.error(f"🚨 {sel_intern}: 미달 과목 → {', '.join(sorted(miss))}")
                st.dataframe(pd.DataFrame(detail_rows), use_container_width=True, hide_index=True)

    # ── 관리자 탭3: 교환 이력 ─────────────────────────────────────────────────
    with adm_tab3:
        st.subheader("🔄 교환 이력")
        hist_rows = mgr.fetch_history_data()
        if len(hist_rows) < 2:
            st.info("교환 이력이 없습니다.")
        else:
            hist_header = hist_rows[0]
            hist_df = pd.DataFrame(hist_rows[1:], columns=hist_header)
            # 최신순 정렬
            if '날짜시간' in hist_df.columns:
                hist_df = hist_df.sort_values('날짜시간', ascending=False)
            st.dataframe(hist_df, use_container_width=True, hide_index=True, height=500)
            st.caption(f"총 {len(hist_df)}건의 이력")

        st.divider()
        st.subheader("⏳ 현재 대기 중인 요청")
        pending = [r for r in mgr.requests if r.get('status') == 'pending']
        if not pending:
            st.info("대기 중인 요청이 없습니다.")
        else:
            pend_rows = []
            for r in pending:
                pend_rows.append({
                    'ID':    r.get('id', ''),
                    '신청자': r.get('sender', ''),
                    '상대방': r.get('receiver', ''),
                    '턴':    r.get('turn', ''),
                    '체인':  r.get('chain_id', '-'),
                })
            st.dataframe(pd.DataFrame(pend_rows), use_container_width=True, hide_index=True)
            st.caption(f"대기 중 {len(pending)}건")

    # ── 관리자 탭4: 장터 현황 ─────────────────────────────────────────────────
    with adm_tab4:
        st.subheader("📝 장터 게시물 현황")
        all_posts = mgr.market_posts
        if not all_posts:
            st.info("장터 게시물이 없습니다.")
        else:
            posts_df = pd.DataFrame(all_posts)
            # 상태별 필터
            status_filter = st.multiselect(
                "상태 필터", ['활성', '마감', '완료'],
                default=['활성'], key="adm_mkt_filter"
            )
            if status_filter:
                filtered_posts = posts_df[posts_df.get('상태', pd.Series()).isin(status_filter)]
            else:
                filtered_posts = posts_df
            st.dataframe(filtered_posts, use_container_width=True, hide_index=True, height=400)
            # 요약
            c1, c2, c3 = st.columns(3)
            c1.metric("전체", len(all_posts))
            c2.metric("활성", len([p for p in all_posts if p.get('상태') == '활성']))
            c3.metric("완료/마감", len([p for p in all_posts if p.get('상태') in ('완료','마감')]))

    # ── 관리자 탭5: 비밀번호 관리 ─────────────────────────────────────────────
    with adm_tab5:
        st.subheader("🔑 비밀번호 관리")
        st.warning("⚠️ 비밀번호를 초기화하면 해당 인턴은 다음 로그인 시 변경 안내를 받습니다.")

        all_users = list(mgr.df.index) + ['ADMIN']
        sel_user_pw = st.selectbox("비밀번호를 초기화할 인턴", all_users, key="adm_pw_sel")
        col_pw1, col_pw2 = st.columns(2)
        new_pw_adm = col_pw1.text_input("새 비밀번호", type="password", key="adm_new_pw",
                                         placeholder="기본값: 1234")
        if col_pw2.button("🔄 비밀번호 초기화", type="primary", use_container_width=True,
                           key="adm_pw_reset"):
            target_pw = new_pw_adm.strip() if new_pw_adm.strip() else '1234'
            ok, msg = mgr.update_password_in_sheet(sel_user_pw, target_pw)
            if ok:
                st.success(f"✅ {sel_user_pw}의 비밀번호가 **{target_pw}** 로 변경되었습니다.")
            else:
                st.error(msg)

        st.divider()
        st.subheader("📋 현재 비밀번호 목록")
        st.caption("비밀번호가 초기값(1234)인 인턴을 확인하세요.")
        pw_rows = []
        for name, pw in mgr.passwords.items():
            pw_rows.append({
                '이름': name,
                '비밀번호 상태': '🔴 초기 비밀번호' if pw == '1234' else '🟢 변경됨',
            })
        if pw_rows:
            st.dataframe(pd.DataFrame(pw_rows), use_container_width=True, hide_index=True)

    # ── 관리자 탭6: 휴가 배정 ──────────────────────────────────────────────────
    with adm_tab6:
        st.subheader("📅 인턴 휴가 배정")
        _num_sort = lambda turns: sorted(turns, key=lambda x: int(x.replace('턴', '')))
        st.info(
            f"**1차 휴가 기간**: {', '.join(_num_sort(VACATION_PERIOD_1))}  |  "
            f"**2차 휴가 기간**: {', '.join(_num_sort(VACATION_PERIOD_2))}\n\n"
            "휴가 타입: **A1-A4** (A그룹 IM, 1-4주차) / **B1-B4** (B그룹 EMC, 1-4주차) / **C1-C4** (C그룹 기타분당·파견, 1-4주차)"
        )

        if mgr.df.empty:
            st.warning("스케줄 데이터가 없습니다.")
        else:
            all_interns = list(mgr.df.index)

            # ── 현재 배정 현황 표 ─────────────────────────────────────────────
            st.subheader("📋 현재 휴가 배정 현황")
            vac_rows = []
            for intern in all_interns:
                vac = mgr.get_intern_vacation(intern)
                v1  = vac['1차']
                v2  = vac['2차']
                # 분당 근무 수도 함께 표시
                bc  = mgr.count_bundang(intern)
                vac_rows.append({
                    '이름':      intern,
                    '1차 턴':    v1['turn'] if v1 else '미배정',
                    '1차 타입':  v1['type'] if v1 else '-',
                    '2차 턴':    v2['turn'] if v2 else '미배정',
                    '2차 타입':  v2['type'] if v2 else '-',
                    '분당 근무': f"{bc}개 {'✅' if bc >= BUNDANG_MIN_TURNS else '🚨'}",
                })
            vac_df = pd.DataFrame(vac_rows)
            st.dataframe(vac_df, use_container_width=True, hide_index=True)

            # ── 휴가 배정 통계 ──────────────────────────────────────────────
            st.markdown("##### 📊 휴가 배정 통계")
            # 턴별 / 그룹별 인원 집계
            turn_stats = {}   # {(period, turn): count}
            group_stats = {}  # {(period, group): count}
            assigned_cnt = 0
            unassigned_cnt = 0

            for intern in all_interns:
                vac = mgr.get_intern_vacation(intern)
                for period in ('1차', '2차'):
                    v = vac[period]
                    if v and v.get('turn') and v.get('type'):
                        assigned_cnt += 1
                        t_key = (period, v['turn'])
                        turn_stats[t_key] = turn_stats.get(t_key, 0) + 1
                        grp = mgr.get_vacation_group(v['type'])
                        g_key = (period, grp)
                        group_stats[g_key] = group_stats.get(g_key, 0) + 1
                    else:
                        unassigned_cnt += 1

            sc1, sc2 = st.columns(2)
            with sc1:
                st.caption(f"배정 완료: {assigned_cnt}건 / 미배정: {unassigned_cnt}건")
                # 턴별 인원 표
                _ns = lambda turns: sorted(turns, key=lambda x: int(x.replace('턴', '')))
                t_rows = []
                for period in ('1차', '2차'):
                    period_turns = _ns(VACATION_PERIOD_1 if period == '1차' else VACATION_PERIOD_2)
                    for t in period_turns:
                        cnt = turn_stats.get((period, t), 0)
                        t_rows.append({'기간': period, '턴': t, '인원': cnt})
                if t_rows:
                    st.markdown("**턴별 휴가 인원**")
                    st.dataframe(pd.DataFrame(t_rows), use_container_width=True,
                                 hide_index=True, height=320)

            with sc2:
                # 그룹별 인원 표
                g_labels = {'A': 'A (IM)', 'B': 'B (EMC)', 'C': 'C (기타/파견)'}
                g_rows = []
                for period in ('1차', '2차'):
                    for grp in ('A', 'B', 'C'):
                        cnt = group_stats.get((period, grp), 0)
                        g_rows.append({'기간': period, '그룹': g_labels[grp], '인원': cnt})
                if g_rows:
                    st.markdown("**그룹별 휴가 인원**")
                    st.dataframe(pd.DataFrame(g_rows), use_container_width=True,
                                 hide_index=True, height=250)

            st.divider()

            # ── 전체 자동 배정 (미리보기 → 확정) ──────────────────────────────
            st.subheader("🤖 전체 자동 배정")
            st.caption(
                "모든 인턴의 1차·2차 휴가를 자동으로 배정합니다.  \n"
                "우선순위: **A(IM)** → **B(EMC)** → **C(기타 분당/파견)**  \n"
                "먼저 **미리보기**로 결과를 확인한 후 확정할 수 있습니다."
            )

            if st.session_state.vacation_preview is None:
                # ── 미리보기 버튼 ─────────────────────────────────────────
                if st.button("🔍 전체 자동 배정 미리보기", type="primary",
                             use_container_width=True, key="adm_preview_all"):
                    with st.spinner("전체 인턴 휴가 자동 배정 미리보기 중..."):
                        preview = mgr.preview_vacation_assignments()
                    st.session_state.vacation_preview = preview
                    st.rerun()
            else:
                # ── 미리보기 결과 표시 ────────────────────────────────────
                preview = st.session_state.vacation_preview
                summary = preview['summary']

                # 요약 지표
                mc1, mc2, mc3, mc4 = st.columns(4)
                mc1.metric("전체 인턴", summary['total'])
                mc2.metric("✅ 완전 배정", summary['ok_count'])
                mc3.metric("⚠️ 부분 배정", summary['partial_count'])
                mc4.metric("❌ 실패", summary['fail_count'])

                # 배정 미리보기 표
                st.markdown("##### 📋 배정 미리보기")
                prev_rows = []
                for r in preview['results']:
                    p1 = r['periods'].get('1차', {})
                    p2 = r['periods'].get('2차', {})
                    prev_rows.append({
                        '이름': r['name'],
                        '1차 턴': p1.get('turn') or '-',
                        '1차 과목': p1.get('dept') or '-',
                        '1차 타입': p1.get('vac_type') or '-',
                        '1차': '✅' if p1.get('status') == 'ok' else '❌',
                        '2차 턴': p2.get('turn') or '-',
                        '2차 과목': p2.get('dept') or '-',
                        '2차 타입': p2.get('vac_type') or '-',
                        '2차': '✅' if p2.get('status') == 'ok' else '❌',
                    })
                st.dataframe(pd.DataFrame(prev_rows),
                             use_container_width=True, hide_index=True)

                # 실패 상세 + 해결 방안
                failed = [r for r in preview['results']
                          if any(r['periods'].get(p, {}).get('status') == 'fail'
                                 for p in ('1차', '2차'))]
                if failed:
                    st.markdown("##### ⚠️ 배정 실패 상세 및 해결 방안")
                    for entry in failed:
                        fail_ps = [p for p in ('1차', '2차')
                                   if entry['periods'].get(p, {}).get('status') == 'fail']
                        with st.expander(
                            f"❌ **{entry['name']}** — {', '.join(fail_ps)} 실패",
                            expanded=False
                        ):
                            for period in fail_ps:
                                pd_ = entry['periods'][period]
                                st.markdown(f"**{period} 실패 진단:**")
                                for tg in pd_.get('tried_groups', []):
                                    st.markdown(
                                        f"- **{tg['group']}그룹 ({tg['group_label']})**: "
                                        f"{tg['error']}")
                                    if tg.get('suggestion'):
                                        st.info(f"💡 **제안**: {tg['suggestion']}")

                # 확정 / 취소 버튼
                col_ok, col_cancel = st.columns(2)
                assignable = summary['ok_count'] + summary['partial_count']
                with col_ok:
                    if st.button(f"✅ 확정 적용 ({assignable}명)",
                                 type="primary", use_container_width=True,
                                 key="adm_confirm_preview",
                                 disabled=assignable == 0):
                        with st.spinner("휴가 배정 확정 적용 중..."):
                            apply_results = mgr.apply_vacation_preview(preview)
                        st.session_state.vacation_preview = None
                        st.rerun()
                with col_cancel:
                    if st.button("🔙 취소", use_container_width=True,
                                 key="adm_cancel_preview"):
                        st.session_state.vacation_preview = None
                        st.rerun()

            st.divider()

            # ── 개별 배정 폼 ──────────────────────────────────────────────────
            st.subheader("✏️ 휴가 배정 / 수정")
            st.caption("타입만 선택하면 → 스케줄에서 해당 과목(IM/EMC/파견) 턴을 자동 탐지합니다.")
            sel_vac_intern = st.selectbox("인턴 선택", all_interns, key="adm_vac_intern")

            if sel_vac_intern:
                cur_vac = mgr.get_intern_vacation(sel_vac_intern)

                # 해당 인턴의 휴가기간 턴 요약 표시 (참고용)
                with st.expander(f"📋 {sel_vac_intern} 휴가기간 스케줄", expanded=True):
                    sched_rows = []
                    for col in mgr.df.columns:
                        if col not in VACATION_PERIOD_1 | VACATION_PERIOD_2:
                            continue
                        val = mgr.df.loc[sel_vac_intern, col] if sel_vac_intern in mgr.df.index else '-'
                        loc, dept = mgr.parse_cell(val) if val and str(val) not in ('None','') else (None,None)
                        period_tag = '1차' if col in VACATION_PERIOD_1 else '2차'
                        sched_rows.append({
                            '기간': period_tag, '턴': col, '값': val or '-',
                            '과목': dept or '-',
                            '지역': loc or '분당',
                            '휴가 가능': (
                                '❌ 진로탐색(불가)' if dept and '진로탐색' in dept else
                                '✅ A타입(IM)' if dept == 'IM' else
                                '✅ B타입(EMC)' if dept == 'EMC' else
                                '✅ C타입(파견병원)' if loc in LOCATIONS else
                                '✅ C타입(기타분당)' if loc == DEFAULT_LOCATION else '—'
                            )
                        })
                    st.dataframe(pd.DataFrame(sched_rows), use_container_width=True, hide_index=True)

                cv1, cv2 = st.columns(2)
                all_types = VACATION_TYPES

                # 1차 휴가
                with cv1:
                    st.markdown("**1차 휴가** (4~7턴 중 자동 탐지)")
                    def_p1ty = cur_vac['1차']['type'] if cur_vac['1차'] else 'A1'
                    p1_type_idx = all_types.index(def_p1ty) if def_p1ty in all_types else 0
                    sel_p1_type = st.selectbox(
                        "1차 타입 (A=IM / B=EMC / C=기타분당·파견)", all_types,
                        index=p1_type_idx, key="adm_p1_type",
                        help="A1-A4: IM 턴에서 휴가 (1-4주차) | B1-B4: EMC 턴에서 휴가 (1-4주차) | C1-C4: 기타 분당·파견병원에서 휴가 (1-4주차)"
                    )
                    # 미리보기: 어느 턴이 될지 표시
                    grp1 = mgr.get_vacation_group(sel_p1_type)
                    prev_t1, prev_d1, prev_e1 = mgr.auto_derive_vacation_turn(sel_vac_intern, '1차', grp1)
                    if prev_t1:
                        st.info(f"🔍 자동 탐지: **{prev_t1}** ({prev_d1 or '파견'})")
                    else:
                        st.warning(f"⚠️ {prev_e1}")
                    c_set1, c_clr1 = st.columns(2)
                    if c_set1.button("💾 1차 저장", use_container_width=True, key="adm_vac_set1",
                                     disabled=prev_t1 is None):
                        ok, msg = mgr.set_intern_vacation(sel_vac_intern, '1차', sel_p1_type)
                        if ok: st.success(msg)
                        else:  st.error(msg)
                    if c_clr1.button("🗑️ 1차 초기화", use_container_width=True, key="adm_vac_clr1"):
                        _, msg = mgr.clear_intern_vacation(sel_vac_intern, '1차')
                        st.success(msg)
                    if cur_vac['1차']:
                        st.caption(f"현재: {cur_vac['1차']['turn']} / {cur_vac['1차']['type']}")

                # 2차 휴가
                with cv2:
                    st.markdown("**2차 휴가** (8~11턴 중 자동 탐지)")
                    def_p2ty = cur_vac['2차']['type'] if cur_vac['2차'] else 'A1'
                    p2_type_idx = all_types.index(def_p2ty) if def_p2ty in all_types else 0
                    sel_p2_type = st.selectbox(
                        "2차 타입 (A=IM / B=EMC / C=기타분당·파견)", all_types,
                        index=p2_type_idx, key="adm_p2_type",
                        help="A1-A4: IM 턴에서 휴가 (1-4주차) | B1-B4: EMC 턴에서 휴가 (1-4주차) | C1-C4: 기타 분당·파견병원에서 휴가 (1-4주차)"
                    )
                    grp2 = mgr.get_vacation_group(sel_p2_type)
                    prev_t2, prev_d2, prev_e2 = mgr.auto_derive_vacation_turn(sel_vac_intern, '2차', grp2)
                    if prev_t2:
                        st.info(f"🔍 자동 탐지: **{prev_t2}** ({prev_d2 or '파견'})")
                    else:
                        st.warning(f"⚠️ {prev_e2}")
                    c_set2, c_clr2 = st.columns(2)
                    if c_set2.button("💾 2차 저장", use_container_width=True, key="adm_vac_set2",
                                     disabled=prev_t2 is None):
                        ok, msg = mgr.set_intern_vacation(sel_vac_intern, '2차', sel_p2_type)
                        if ok: st.success(msg)
                        else:  st.error(msg)
                    if c_clr2.button("🗑️ 2차 초기화", use_container_width=True, key="adm_vac_clr2"):
                        _, msg = mgr.clear_intern_vacation(sel_vac_intern, '2차')
                        st.success(msg)
                    if cur_vac['2차']:
                        st.caption(f"현재: {cur_vac['2차']['turn']} / {cur_vac['2차']['type']}")

            st.divider()

            # ── 필수과목 + 분당 근무 통합 현황 ──────────────────────────────────
            st.subheader("🏥 필수과목 · 분당 근무 충족 현황")
            compliance_rows = []
            for intern in all_interns:
                v_ok, missing = mgr.validate_intern(intern)
                bc = mgr.count_bundang(intern)
                b_ok = bc >= BUNDANG_MIN_TURNS
                compliance_rows.append({
                    '이름': intern,
                    '필수과목': '✅ 충족' if v_ok else '🚨 미충족',
                    '누락 과목': ', '.join(sorted(missing)) if missing else '-',
                    '분당 턴': f"{bc}개",
                    '분당 충족': f"✅ ({bc}/{BUNDANG_MIN_TURNS})" if b_ok
                               else f"🚨 부족 ({bc}/{BUNDANG_MIN_TURNS})",
                    '종합': '✅' if (v_ok and b_ok) else '🚨',
                })
            st.dataframe(pd.DataFrame(compliance_rows),
                         use_container_width=True, hide_index=True)
            # 요약
            all_ok = sum(1 for r in compliance_rows if r['종합'] == '✅')
            st.caption(f"전체 {len(compliance_rows)}명 중 **{all_ok}명** 모든 조건 충족 / "
                       f"**{len(compliance_rows) - all_ok}명** 미충족")

            st.divider()

            # ── 구글 시트 휴가반영 동기화 ──────────────────────────────────────
            st.subheader("📤 구글 시트 업데이트")
            st.caption(
                f"휴가 배정 결과를 **'{VACATION_SHEET_NAME}'** 시트에 씁니다.  \n"
                "휴가 턴 셀은 **과목↵타입** 형식으로 표기됩니다 (예: `IM↵A1`)."
            )
            if not mgr.sheet_connected:
                st.warning("⚠️ 구글 시트에 연결되어 있지 않아 업데이트할 수 없습니다.")
            else:
                if st.button("📤 휴가반영 시트 업데이트", type="primary",
                             use_container_width=True, key="adm_sync_vac_sheet"):
                    with st.spinner(f"'{VACATION_SHEET_NAME}' 시트 업데이트 중..."):
                        ok, msg = mgr.sync_vacation_sheet()
                    if ok:
                        st.success(msg)
                    else:
                        st.error(msg)

    st.stop()

# ── 첫 로그인: 비밀번호 변경 강제 안내 ──────────────────────────────────────
if st.session_state.get('force_pw_change'):
    st.title("🔑 비밀번호 변경")
    st.warning("⚠️ 초기 비밀번호(1234)를 사용 중입니다. 보안을 위해 비밀번호를 변경해주세요.")
    fp1 = st.text_input("새 비밀번호 (4자 이상)", type="password", key="fp_pw1")
    fp2 = st.text_input("비밀번호 확인",          type="password", key="fp_pw2")
    c_skip, c_change = st.columns(2)
    if c_skip.button("나중에 변경", use_container_width=True):
        st.session_state.force_pw_change = False
        st.rerun()
    if c_change.button("✅ 변경하기", type="primary", use_container_width=True):
        if len(fp1) < 4:
            st.error("4자 이상이어야 합니다.")
        elif fp1 != fp2:
            st.error("비밀번호가 일치하지 않습니다.")
        else:
            ok, msg = mgr.update_password_in_sheet(user, fp1)
            if ok:
                st.session_state.force_pw_change = False
                st.success("✅ 비밀번호가 변경되었습니다!")
                st.rerun()
            else:
                st.error(msg)
    st.stop()

# ══════════════════════════════════════════════════════════════════════════════
#  SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:

    # ── 이름 + 필수과목 현황 (compact) ────────────────────────────────────────
    valid_sb, missing_sb = mgr.validate_intern(user)
    dept_badge = "✅ 충족" if valid_sb else f"🚨 {len(missing_sb)}개 미달"
    st.markdown(f"### 👤 {user}")
    if valid_sb:
        st.caption("✅ 필수과목 모두 충족")
    else:
        st.error(f"🚨 미달: {', '.join(sorted(missing_sb))}")

    # ── 비밀번호 변경 (compact) ────────────────────────────────────────────────
    with st.expander("🔑 비밀번호 변경"):
        old_pw  = st.text_input("현재",        type="password", key="old_pw")
        new_pw  = st.text_input("새 비밀번호", type="password", key="new_pw")
        new_pw2 = st.text_input("확인",        type="password", key="new_pw2")
        if st.button("변경 적용", key="btn_pw"):
            if not mgr.check_password(user, old_pw):
                st.error("현재 비밀번호 오류")
            elif len(new_pw) < 4:
                st.error("4자 이상이어야 합니다")
            elif new_pw != new_pw2:
                st.error("비밀번호 불일치")
            else:
                ok, msg = mgr.update_password_in_sheet(user, new_pw)
                st.success(msg) if ok else st.error(msg)

    col_ref, col_lo, col_mode = st.columns(3)
    if col_ref.button("🔄 새로고침", use_container_width=True):
        mgr.load_db()
        st.rerun()
    if col_lo.button("🚪 로그아웃", use_container_width=True):
        del st.session_state.user_id
        st.rerun()
    _sb_mobile = st.session_state.layout_mode == 'mobile'
    if col_mode.button("💻 PC 버전으로 전환" if _sb_mobile else "📱 모바일 버전으로 전환",
                       use_container_width=True, key="sb_mode_toggle"):
        st.session_state.layout_mode = 'pc' if _sb_mobile else 'mobile'
        st.rerun()

    st.divider()

    # ── 요청 내역 ─────────────────────────────────────────────────────────────
    # 일반 요청 + 체인 요청 분리
    inbox_normal = [r for r in mgr.requests
                    if r['receiver'] == user and r['status'] == 'pending' and 'chain_id' not in r]
    inbox_chain  = [r for r in mgr.requests
                    if r['receiver'] == user and r['status'] == 'pending' and 'chain_id' in r]
    inbox_sb     = inbox_normal + inbox_chain
    sent_pend_sb = [r for r in mgr.requests
                    if r['sender'] == user and r['status'] in ('pending', 'chain_accepted')]
    with st.expander(
        f"📩 요청 내역  (받은 {len(inbox_sb)} · 보낸 {len(sent_pend_sb)})",
        expanded=len(inbox_sb) > 0
    ):
        tab_in, tab_out = st.tabs(["📥 받은 요청", "📤 보낸 요청"])

        with tab_in:
            if not inbox_sb:
                st.info("받은 요청이 없습니다.")
            else:
                # 일반 요청
                for req in inbox_normal:
                    snd_v = mgr.df.loc[req['sender'], req['turn']] if req['sender'] in mgr.df.index else '?'
                    my_v  = mgr.df.loc[user, req['turn']]          if user          in mgr.df.index else '?'
                    st.write(f"**{req['sender']}** | {req['turn']}")
                    st.caption(f"상대: `{snd_v}` ↔ 나: `{my_v}`  _{req['timestamp'][:10]}_")
                    if req.get('message'):
                        st.info(f"💬 {req['message']}")
                    ca, cb = st.columns(2)
                    if ca.button("✅ 수락", key=f"sb_acc_{req['id']}", type="primary", use_container_width=True):
                        succ, msg = mgr.process_request(req['id'], 'accept')
                        if succ: st.success(msg); st.rerun()
                        else:    st.error(msg)
                    if cb.button("❌ 거절", key=f"sb_rej_{req['id']}", use_container_width=True):
                        mgr.process_request(req['id'], 'reject')
                        st.rerun()
                    st.divider()

                # 체인 요청 (chain_id별 그룹)
                chain_groups = {}
                for req in inbox_chain:
                    cid = req['chain_id']
                    chain_groups.setdefault(cid, []).append(req)

                for cid, reqs in chain_groups.items():
                    st.markdown(f"🔗 **복합 교환 요청** from **{reqs[0]['sender']}**")
                    chain_all = [r for r in mgr.requests if r.get('chain_id') == cid]
                    total = len(chain_all)
                    accepted = sum(1 for r in chain_all if r['status'] == 'chain_accepted')
                    st.caption(f"체인 {total}건 중 {accepted}건 수락됨 | 모두 수락 시 일괄 실행")
                    if reqs[0].get('message'):
                        st.info(f"💬 {reqs[0]['message']}")
                    for req in reqs:
                        snd_v = mgr.df.loc[req['sender'], req['turn']] if req['sender'] in mgr.df.index else '?'
                        my_v  = mgr.df.loc[user, req['turn']]          if user          in mgr.df.index else '?'
                        st.write(f"  • **{req['turn']}**: `{snd_v}` ↔ 나: `{my_v}`")
                    ca, cb = st.columns(2)
                    # 체인은 내게 해당하는 건만 일괄 수락/거절
                    my_chain_reqs = [r for r in reqs if r['status'] == 'pending']
                    if ca.button("✅ 수락", key=f"sb_chacc_{cid}", type="primary", use_container_width=True):
                        last_msg = ""
                        for r in my_chain_reqs:
                            succ, last_msg = mgr.process_chain_action(cid, r['id'], 'accept', user)
                        st.success(last_msg)
                        st.rerun()
                    if cb.button("❌ 거절", key=f"sb_chrej_{cid}", use_container_width=True):
                        for r in my_chain_reqs:
                            mgr.process_chain_action(cid, r['id'], 'reject', user)
                        st.rerun()
                    st.divider()

        with tab_out:
            sent_all_sb = [r for r in mgr.requests if r['sender'] == user]
            if not sent_all_sb:
                st.info("보낸 요청이 없습니다.")
            else:
                lbl_sb = {'pending': '⏳', 'accepted': '✅', 'rejected': '❌',
                          'cancelled': '🚫', 'chain_accepted': '🔗✅', 'chain_rejected': '🔗❌'}
                # 체인 그룹 표시
                shown_chains = set()
                for r in reversed(sent_all_sb):
                    cid = r.get('chain_id')
                    if cid:
                        if cid in shown_chains:
                            continue
                        shown_chains.add(cid)
                        chain_all = [x for x in mgr.requests if x.get('chain_id') == cid]
                        statuses = [x['status'] for x in chain_all]
                        if all(s == 'accepted' for s in statuses):
                            chain_icon = "✅"
                        elif any(s in ('chain_rejected',) for s in statuses):
                            chain_icon = "❌"
                        elif any(s == 'chain_accepted' for s in statuses):
                            chain_icon = "🔗⏳"
                        else:
                            chain_icon = "⏳"
                        st.write(f"{chain_icon} **복합 교환** ({len(chain_all)}건)")
                        for x in chain_all:
                            xi = lbl_sb.get(x['status'], x['status'])
                            st.caption(f"  {xi} {x['turn']} → {x['receiver']}  `{x.get('val_sender','')}` ↔ `{x.get('val_receiver','')}`")
                        if all(s == 'pending' for s in statuses):
                            if st.button("취소", key=f"sb_chcancel_{cid}", use_container_width=True):
                                for x in chain_all:
                                    if x['status'] == 'pending':
                                        mgr.cancel_request(x['id'], user)
                                st.rerun()
                    else:
                        icon_sb = lbl_sb.get(r['status'], r['status'])
                        st.write(f"{icon_sb} **{r['turn']}** → {r['receiver']}")
                        st.caption(
                            f"`{r.get('val_sender','')}` ↔ `{r.get('val_receiver','')}`  "
                            f"_{r['timestamp'][:10]}_"
                        )
                        if r.get('message'):
                            st.caption(f"💬 {r['message']}")
                        if r['status'] == 'pending':
                            if st.button("취소", key=f"sb_cancel_{r['id']}", use_container_width=True):
                                ok, msg = mgr.cancel_request(r['id'], user)
                                if ok: st.success(msg); st.rerun()
                                else:  st.error(msg)
                    st.divider()

    # ── 교환이력 ──────────────────────────────────────────────────────────────
    with st.expander("📜 교환이력"):
        hist_rows = mgr.fetch_history_data()
        if len(hist_rows) > 1:
            hdf_sb = pd.DataFrame(hist_rows[1:], columns=hist_rows[0])
            my_h_sb = hdf_sb[
                (hdf_sb.get('신청자', pd.Series(dtype=str)) == user) |
                (hdf_sb.get('상대방', pd.Series(dtype=str)) == user)
            ]
            th1, th2 = st.tabs([f"내 이력 ({len(my_h_sb)})", f"전체 ({len(hdf_sb)})"])
            with th1:
                st.dataframe(my_h_sb.iloc[::-1].reset_index(drop=True),
                             use_container_width=True, hide_index=True, height=200)
            with th2:
                st.dataframe(hdf_sb.iloc[::-1].reset_index(drop=True),
                             use_container_width=True, hide_index=True, height=200)
        else:
            st.info("기록된 교환이력이 없습니다.")

    st.divider()

    # ── 교환 시뮬레이션 ───────────────────────────────────────────────────────
    st.subheader("🧪 교환 시뮬레이션")
    sim_mode = st.radio(
        "모드",
        ["🔄 특정 턴 교환", "🎯 특정 턴 받기", "🔗 복합 교환 탐색"],
        key="sim_mode_radio",
        label_visibility="collapsed"
    )

    if sim_mode == "🔄 특정 턴 교환":
        st.caption("특정 턴을 교환했을 때 가능한 파트너를 탐색합니다.")
        if not mgr.df.empty:
            sim_turns_avail = [c for c in mgr.df.columns if c not in LOCKED_TURNS]
            sim_turn = st.selectbox("턴 선택", sim_turns_avail, key="sim_turn_sel")
            if user in mgr.df.index and sim_turn in mgr.df.columns:
                sim_my_val = mgr.df.loc[user, sim_turn]
                const_sb   = mgr.get_exchange_constraints(user, sim_turn)
                st.metric(f"내 {sim_turn}", sim_my_val or "(없음)")
                if const_sb:
                    if const_sb['is_free']:
                        st.success("✅ 제약 없음")
                    else:
                        st.error(f"필수: {', '.join(sorted(const_sb['required_to_receive']))}")
                        if const_sb['already_covered']:
                            st.caption(f"확보: {', '.join(sorted(const_sb['already_covered']))}")
                only_v1 = st.checkbox("가능한 것만 보기", value=False, key="sim_v1")
                with st.spinner("계산 중..."):
                    results_s1 = mgr.simulate_exchanges(user, sim_turn)
                if not results_s1:
                    st.info("비교할 파트너가 없습니다.")
                else:
                    disp_s1  = [r for r in results_s1 if r['valid']] if only_v1 else results_s1
                    valid_s1 = sum(1 for r in results_s1 if r['valid'])
                    st.caption(f"전체 {len(results_s1)}명 중 가능: **{valid_s1}명**")
                    for r in disp_s1:
                        icon_r = "✅" if r['valid'] else "❌"
                        col_a, col_b = st.columns([3, 1])
                        col_a.write(f"{icon_r} **{r['partner']}**{'  ⏳' if r['has_pending'] else ''}")
                        col_a.caption(
                            f"`{r['partner_val']}`" +
                            (f"  ⚠️ {' / '.join(r['reasons'])}" if r['reasons'] else "")
                        )
                        if r['valid'] and not r['has_pending']:
                            if col_b.button("요청", key=f"sbsim1_{r['partner']}_{sim_turn}", type="primary"):
                                st.session_state.quick_confirm = {
                                    'receiver': r['partner'], 'turn': sim_turn,
                                    'my_val': sim_my_val, 'partner_val': r['partner_val'],
                                }
                                st.rerun()
                        elif r['has_pending']:
                            col_b.caption("⏳")

    elif sim_mode == "🎯 특정 턴 받기":
        st.caption("받고 싶은 과목을 선택하면 해당 과목을 받을 수 있는 모든 조합을 탐색합니다.")
        all_sv = sorted(set(
            str(v).strip() for col in mgr.df.columns
            for v in mgr.df[col].dropna()
            if v and str(v).strip() not in ('None', '', '예비턴')
        )) if not mgr.df.empty else []
        if all_sv:
            target_vs = st.multiselect("받고 싶은 과목/턴 (복수 선택 가능)", all_sv, key="sim_val_sel")
            only_v2   = st.checkbox("가능한 것만 보기", value=False, key="sim_v2")
            if not target_vs:
                st.info("과목/턴을 하나 이상 선택하세요.")
            else:
                with st.spinner("계산 중..."):
                    seen_s2    = set()
                    results_s2 = []
                    for tv in target_vs:
                        for r in mgr.simulate_by_desired_dept(user, tv):
                            key = (r['partner'], r['turn'])
                            if key not in seen_s2:
                                seen_s2.add(key)
                                results_s2.append(r)
                    results_s2.sort(key=lambda x: (not x['valid'], x['turn'], x['partner']))
                if not results_s2:
                    st.info("선택한 과목/턴을 받을 수 있는 조합이 없습니다.")
                else:
                    disp_s2  = [r for r in results_s2 if r['valid']] if only_v2 else results_s2
                    valid_s2 = sum(1 for r in results_s2 if r['valid'])
                    st.caption(f"전체 {len(results_s2)}개 중 가능: **{valid_s2}개**")
                    for r in disp_s2:
                        icon_r = "✅" if r['valid'] else "❌"
                        col_a, col_b = st.columns([3, 1])
                        col_a.write(f"{icon_r} **{r['partner']}**  {r['turn']}{'  ⏳' if r['has_pending'] else ''}")
                        col_a.caption(f"나: `{r['my_val']}` → 받을 턴: `{r['partner_val']}`")
                        if r['reasons']:
                            col_a.caption(f"⚠️ {' / '.join(r['reasons'])}")
                        if r['valid'] and not r['has_pending']:
                            if col_b.button("요청", key=f"sbsim2_{r['partner']}_{r['turn']}", type="primary"):
                                st.session_state.quick_confirm = {
                                    'receiver': r['partner'], 'turn': r['turn'],
                                    'my_val': r['my_val'], 'partner_val': r['partner_val'],
                                }
                                st.rerun()
                        elif r['has_pending']:
                            col_b.caption("⏳")

    else:  # 🔗 복합 교환 탐색
        st.caption("2~3개 턴을 동시에 교환하는 조합을 탐색합니다.")
        st.info("💡 **일괄 요청**: 모든 상대방이 수락해야만 교환이 실행됩니다.")

        if 'chain_results' not in st.session_state:
            st.session_state.chain_results = None

        all_sv_chain = sorted(set(
            str(v).strip() for col in mgr.df.columns
            for v in mgr.df[col].dropna()
            if v and str(v).strip() not in ('None', '', '예비턴')
        )) if not mgr.df.empty else []
        want_depts_chain = st.multiselect("받고 싶은 과목/턴 (복수 선택 가능)", all_sv_chain, key="chain_dept_sel")

        max_sw = st.radio("최대 교환 수", [2, 3], index=0, horizontal=True, key="sim_max_sw")

        if st.button("🔍 복합 탐색", type="primary", key="btn_chain_search"):
            if not want_depts_chain:
                st.warning("과목/턴을 하나 이상 선택하세요.")
            else:
                with st.spinner("복합 교환 탐색 중... (잠시 기다려주세요)"):
                    all_chain = mgr.simulate_multi_swap(user, only_need_multi=True, max_swaps=max_sw)
                # 선택한 과목 중 하나라도 받는 조합 필터
                filtered = [c for c in all_chain
                            if any(s['partner_val'] in want_depts_chain for s in c['swaps'])]
                st.session_state.chain_results = filtered

        chain_res = st.session_state.chain_results
        if chain_res is not None:
            if not chain_res:
                label = ', '.join(f'**{v}**' for v in (want_depts_chain or []))
                st.info(f"{label} 을(를) 받을 수 있는 복합 교환 조합이 없습니다.")
            else:
                st.caption(f"가능한 복합 교환 조합: **{len(chain_res)}개**")
                for idx, combo in enumerate(chain_res):
                    swaps = combo['swaps']
                    alone = combo['alone']
                    alone_tags = []
                    for si, a in enumerate(alone):
                        if not a:
                            alone_tags.append(f"{'①②③'[si]}단독불가")
                    alone_note = f"  ⭐ {', '.join(alone_tags)}" if alone_tags else ""
                    st.write(f"**조합 {idx+1}** ({len(swaps)}건){alone_note}")
                    lines = []
                    for si, s in enumerate(swaps):
                        num = '①②③'[si]
                        lines.append(
                            f"{num} **{s['turn']}**  "
                            f"나: `{s['my_val']}` ↔ **{s['partner']}**: `{s['partner_val']}`"
                        )
                    st.caption("\n".join(lines))
                    if st.button(f"🔗 일괄 요청 ({len(swaps)}건)", key=f"chain_req_{idx}",
                                 type="primary", use_container_width=True):
                        swap_data = [{'receiver': s['partner'], 'turn': s['turn']} for s in swaps]
                        with st.spinner("복합 교환 요청 중..."):
                            mgr.load_db()
                            ok, msg = mgr.add_chain_request(user, swap_data)
                        if ok:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)
                    st.divider()

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN CONTENT
# ══════════════════════════════════════════════════════════════════════════════
st.title("🏥 차병원 인턴 턴표 교환소")

# ── 빠른 요청 컨펌 팝업 다이얼로그 ──────────────────────────────────────────
@st.dialog("⚡ 교환 요청 확인")
def quick_confirm_dialog():
    qc = st.session_state.quick_confirm
    st.info(
        f"**{qc['turn']}** 턴  |  "
        f"나: `{qc['my_val']}` ↔ **{qc['receiver']}**: `{qc['partner_val']}`"
    )
    qc_message = st.text_input(
        "요청 메시지 (선택)", placeholder="교환 사유, 연락처 등", max_chars=100
    )
    col_yes, col_no = st.columns(2)
    if col_yes.button("✅ 요청 보내기", type="primary", use_container_width=True):
        with st.spinner("최신 데이터 확인 중..."):
            mgr.load_db()
        succ, msg = mgr.add_request(user, qc['receiver'], qc['turn'], message=qc_message)
        st.session_state.quick_confirm = None
        if succ:
            st.success(msg)
        else:
            st.error(msg)
        st.rerun()
    if col_no.button("❌ 취소", use_container_width=True):
        st.session_state.quick_confirm = None
        st.rerun()

if st.session_state.quick_confirm is not None:
    quick_confirm_dialog()

# ── 복합 교환 요청 컨펌 팝업 다이얼로그 ─────────────────────────────────────
@st.dialog("📤 복합 교환 요청 확인")
def multi_confirm_dialog():
    items = st.session_state.multi_confirm
    st.write(f"다음 교환 **{len(items)}건**을 신청합니다:")
    for i, it in enumerate(items):
        num = '①②③④⑤'[i] if i < 5 else f'{i+1}.'
        st.write(f"{num} **{it['turn']}** &nbsp; 나: `{it['my_val']}` ↔ **{it['target']}**: `{it['partner_val']}`")
    mc_message = st.text_input("요청 메시지 (선택)", placeholder="교환 사유, 연락처 등",
                               max_chars=100, key="mc_msg")
    col_yes, col_no = st.columns(2)
    if col_yes.button("✅ 요청 보내기", type="primary", use_container_width=True):
        with st.spinner("최신 데이터 확인 및 재검증 중..."):
            mgr.load_db()
            exchanges = [{'target': it['target'], 'turn': it['turn']} for it in items]
            valid, errs = mgr.validate_multi_exchange(user, exchanges)
        if not valid:
            st.error("⚠️ 최신 데이터 기준 재검증 실패:\n" + "\n".join(errs))
        else:
            if len(items) == 1:
                succ, msg = mgr.add_request(user, items[0]['target'],
                                            items[0]['turn'], message=mc_message)
            else:
                swaps = [{'receiver': it['target'], 'turn': it['turn']} for it in items]
                succ, msg = mgr.add_chain_request(user, swaps, message=mc_message)
            st.session_state.multi_confirm = None
            st.session_state.exchange_items = []
            if succ:
                st.success(msg)
            else:
                st.error(msg)
            st.rerun()
    if col_no.button("❌ 취소", use_container_width=True):
        st.session_state.multi_confirm = None
        st.rerun()

if st.session_state.multi_confirm is not None:
    multi_confirm_dialog()

# ──────────────────────────────────────────────────────────────────────────────
# ① 교환 신청
# ──────────────────────────────────────────────────────────────────────────────
st.subheader("📤 교환 신청")
avail_turns = [c for c in mgr.df.columns if c not in LOCKED_TURNS]
others = [u for u in mgr.df.index if u != user]

# 항목이 없으면 기본값 1개 추가
if not st.session_state.exchange_items and others and avail_turns:
    st.session_state.exchange_items.append({
        'id': st.session_state.ei_counter,
        'target': others[0],
        'turn': avail_turns[0]
    })
    st.session_state.ei_counter += 1

# 각 항목 렌더링
to_remove = []
for i, item in enumerate(st.session_state.exchange_items):
    iid = item['id']
    num_label = '①②③④⑤'[i] if i < 5 else f'{i+1}.'
    t_default    = item.get('target', others[0])    if others      else ''
    turn_default = item.get('turn',   avail_turns[0]) if avail_turns else ''
    if t_default    not in others:      t_default    = others[0]      if others      else ''
    if turn_default not in avail_turns: turn_default = avail_turns[0] if avail_turns else ''
    # 1행: 상대방 | 턴 | ✕버튼
    lbl = f"상대방 {num_label}" if len(st.session_state.exchange_items) > 1 else "상대방"
    c1, c2, c4 = st.columns([2, 1.5, 0.5])
    with c1:
        sel_t = st.selectbox(lbl, others,
                             index=others.index(t_default), key=f'ei_t_{iid}')
    with c2:
        sel_turn = st.selectbox("턴", avail_turns,
                                index=avail_turns.index(turn_default), key=f'ei_turn_{iid}')
    st.session_state.exchange_items[i]['target'] = sel_t
    st.session_state.exchange_items[i]['turn']   = sel_turn
    my_v = mgr.df.loc[user,   sel_turn] if user   in mgr.df.index else '?'
    pt_v = mgr.df.loc[sel_t,  sel_turn] if sel_t  in mgr.df.index else '?'
    # 2행: 교환 정보 | 삭제 버튼
    with c4:
        st.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)
        if len(st.session_state.exchange_items) > 1:
            if st.button("✕", key=f'ei_rm_{iid}', help="항목 제거", use_container_width=True):
                to_remove.append(i)
    st.caption(f"나: `{my_v}` ↔ **{sel_t}**: `{pt_v}`")

for idx in reversed(to_remove):
    st.session_state.exchange_items.pop(idx)
if to_remove:
    st.rerun()

# 합산 검증 표시
can_send = False
if st.session_state.exchange_items:
    exchanges = [{'target': it['target'], 'turn': it['turn']}
                 for it in st.session_state.exchange_items]
    can_send, errs = mgr.validate_multi_exchange(user, exchanges)
    if can_send:
        st.session_state.combo_suggestions = None   # 조건 충족 시 제안 초기화
        st.session_state.combo_page = 0
        if len(exchanges) == 1:
            it = st.session_state.exchange_items[0]
            mv = mgr.df.loc[user,         it['turn']] if user          in mgr.df.index else '?'
            pv = mgr.df.loc[it['target'], it['turn']] if it['target']  in mgr.df.index else '?'
            st.info(f"💡 교환 예정: 나의 `{mv}` ↔ **{it['target']}**의 `{pv}`")
        else:
            st.success(f"✅ {len(exchanges)}건 동시 교환 시 조건 충족")
    else:
        st.error("⚠️ 조건 불충족: " + " / ".join(errs))

        # ── 가능한 조합 탐색 ──────────────────────────────────────────────────
        if st.button("🔍 가능한 조합 찾기", key="btn_find_combo"):
            with st.spinner("가능한 조합 탐색 중... (잠시 기다려주세요)"):
                mandatory = [{'target': it['target'], 'turn': it['turn']}
                             for it in st.session_state.exchange_items]
                found = mgr.find_completing_combos(user, mandatory, max_additional=2)
            st.session_state.combo_suggestions = found
            st.session_state.combo_page = 0

        combos = st.session_state.combo_suggestions
        if combos is not None:
            if not combos:
                st.info("현재 선택 교환을 포함한 가능한 조합이 없습니다.")
            else:
                # 추가 교환 수 기준 정렬 (적은 것 먼저)
                combos_sorted = sorted(combos, key=lambda c: len(c['additional']))
                PAGE_SIZE = 10
                total      = len(combos_sorted)
                total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
                # 페이지가 범위를 벗어나면 보정
                if st.session_state.combo_page >= total_pages:
                    st.session_state.combo_page = 0
                page = st.session_state.combo_page
                start = page * PAGE_SIZE
                page_combos = combos_sorted[start: start + PAGE_SIZE]

                # 헤더: 총 개수 + 페이지 표시
                h1, h2 = st.columns([4, 2])
                h1.caption(
                    f"💡 현재 선택 유지 + 추가 교환으로 가능한 조합 **{total}개** "
                    f"({start + 1}–{min(start + PAGE_SIZE, total)} 표시)"
                )
                # 페이지 이동 버튼
                pb1, pb2, pb3 = h2.columns([1, 2, 1])
                if pb1.button("◀", key="combo_prev", disabled=(page == 0)):
                    st.session_state.combo_page -= 1
                    st.rerun()
                pb2.markdown(
                    f"<div style='text-align:center;padding-top:6px'>"
                    f"{page + 1} / {total_pages}</div>",
                    unsafe_allow_html=True
                )
                if pb3.button("▶", key="combo_next", disabled=(page >= total_pages - 1)):
                    st.session_state.combo_page += 1
                    st.rerun()

                # 현재 페이지 조합 목록
                for ci, combo in enumerate(page_combos):
                    global_idx = start + ci
                    all_swaps  = combo['all_swaps']
                    lines        = []
                    sender_swaps = []   # 내가 직접 신청하는 교환만
                    for s in all_swaps:
                        if s.get('is_partner_comp'):
                            # 상대방이 제3자와 하는 보완 교환
                            owner = s['comp_owner']
                            lines.append(
                                f"🔄 **보완** **{s['turn']}** &nbsp;"
                                f"**{owner}**: `{s['my_val']}` ↔ **{s['partner']}**: `{s['partner_val']}`"
                                f"  _({owner}↔{s['partner']} 별도 합의 필요)_"
                            )
                        else:
                            tag = "➕ 추가" if s.get('is_additional') else "✓ 선택됨"
                            lines.append(
                                f"**{tag}** **{s['turn']}** &nbsp;"
                                f"나: `{s['my_val']}` ↔ **{s['partner']}**: `{s['partner_val']}`"
                            )
                            sender_swaps.append(s)
                    cc1, cc2 = st.columns([5, 1])
                    cc1.write("  \n".join(lines))
                    if sender_swaps and cc2.button("⬆️ 설정", key=f"cset_{global_idx}",
                                  use_container_width=True, help="이 조합으로 교환 항목 설정"):
                        st.session_state.exchange_items = []
                        for s in sender_swaps:   # 내 교환만 설정
                            st.session_state.exchange_items.append({
                                'id':     st.session_state.ei_counter,
                                'target': s['partner'],
                                'turn':   s['turn']
                            })
                            st.session_state.ei_counter += 1
                        st.session_state.combo_suggestions = None
                        st.session_state.combo_page = 0
                        st.rerun()
                    st.divider()

col_add, col_send = st.columns([1, 3])
if col_add.button("➕ 항목 추가", key="ei_add"):
    if others and avail_turns:
        st.session_state.exchange_items.append({
            'id': st.session_state.ei_counter,
            'target': others[0],
            'turn': avail_turns[0]
        })
        st.session_state.ei_counter += 1
        st.rerun()

if col_send.button("📨 요청 보내기", type="primary", use_container_width=True,
                   disabled=not can_send):
    confirm_items = []
    for it in st.session_state.exchange_items:
        mv = mgr.df.loc[user,       it['turn']] if user         in mgr.df.index else '?'
        pv = mgr.df.loc[it['target'], it['turn']] if it['target'] in mgr.df.index else '?'
        confirm_items.append({'target': it['target'], 'turn': it['turn'],
                               'my_val': mv, 'partner_val': pv})
    st.session_state.multi_confirm = confirm_items
    st.rerun()

# ──────────────────────────────────────────────────────────────────────────────
# ② 전체 스케줄 (교환 신청 바로 아래)
# ──────────────────────────────────────────────────────────────────────────────
st.subheader("📋 전체 스케줄")
sel_targets     = list(dict.fromkeys(it['target'] for it in st.session_state.exchange_items)) \
                  if st.session_state.exchange_items else []
highlight_pairs = ({(user, it['turn']) for it in st.session_state.exchange_items} |
                   {(it['target'], it['turn']) for it in st.session_state.exchange_items}) \
                  if st.session_state.exchange_items else set()

remain     = [x for x in mgr.df.index if x != user and x not in sel_targets]
display_df = mgr.df.reindex([user] + sel_targets + remain)

def style_table(row):
    styles = []
    for col in row.index:
        s = ""
        if row.name == user:
            s += "background-color:#fffde7;font-weight:bold;"
        elif row.name in sel_targets:
            s += "background-color:#e3f2fd;"
        if (row.name, col) in highlight_pairs:
            s += "border:3px solid #e53935;background-color:#ffcdd2;"
        styles.append(s)
    return styles

st.dataframe(
    display_df.style.apply(style_table, axis=1),
    use_container_width=True, height=500
)

st.markdown("---")

# ──────────────────────────────────────────────────────────────────────────────
# ③ 교환 장터
# ──────────────────────────────────────────────────────────────────────────────
st.subheader("🏪 교환 장터")

tab_browse, tab_post, tab_mine = st.tabs(["🔍 장터 둘러보기", "📝 내 턴 올리기", "🗂️ 내 게시물"])

active_posts = [p for p in mgr.market_posts if p.get('상태') == '활성']
others_posts = [p for p in active_posts if p.get('등록자') != user]
my_posts     = [p for p in active_posts if p.get('등록자') == user]

# ── 장터: 둘러보기 ──────────────────────────────────────────────────────────
with tab_browse:
    if not others_posts:
        st.info("현재 다른 사람의 교환 요청이 없습니다.")
    else:
        st.caption(f"활성 게시물 {len(others_posts)}건")
        for post in others_posts:
            poster    = post.get('등록자', '')
            give_turn = post.get('주고싶은턴', '')
            give_val  = post.get('주고싶은값', '')
            want_str  = post.get('받고싶은과', '무관')
            post_msg  = post.get('메시지', '')
            post_time = post.get('등록시각', '')
            post_id   = post.get('등록ID', '')
            compatibilities = mgr.get_market_compatibilities(user, post)
            valid_cnt = sum(1 for c in compatibilities if c['valid'])
            has_valid = valid_cnt > 0
            if give_turn == '아무턴':
                turn_label = '아무 턴'
            elif give_turn:
                turn_label = give_turn
            else:
                turn_label = '무관'
            with st.expander(
                f"{'🟢' if has_valid else '🔴'} **{poster}** | "
                f"줄 것: {turn_label}{(' `'+give_val+'`') if give_val else ''} | "
                f"원하는 과목: {want_str or '무관'} | "
                f"{'✅ '+str(valid_cnt)+'개 조합' if has_valid else '❌ 단독 불가 (복합 탐색 가능)'}",
                expanded=has_valid
            ):
                if post_msg:
                    st.info(f"💬 {post_msg}")
                st.caption(f"📅 등록: {post_time}")
                if not compatibilities:
                    st.warning("교환 가능한 조합이 없거나 데이터가 부족합니다.")
                else:
                    for combo in compatibilities:
                        t        = combo['turn']
                        p_val    = combo['poster_val']
                        v_val    = combo['viewer_val']
                        is_valid = combo['valid']
                        reasons  = combo['reasons']
                        has_pend = combo['has_pending']
                        mkt_key  = f"{post_id}__{t}"
                        col_i, col_b = st.columns([5, 1])
                        with col_i:
                            icon = '✅' if is_valid else '❌'
                            st.write(f"{icon} **{t}** — {poster}: `{p_val}` ↔ 나: `{v_val}`")
                            if reasons:
                                st.caption(f"⚠️ {' / '.join(reasons)}")
                        with col_b:
                            if is_valid and not has_pend:
                                if st.button("요청", key=f"mkt_{post_id}_{t}", type="primary"):
                                    my_v_here = (mgr.df.loc[user, t]
                                                 if user in mgr.df.index and t in mgr.df.columns else '')
                                    st.session_state.quick_confirm = {
                                        'receiver': poster, 'turn': t,
                                        'my_val': my_v_here, 'partner_val': p_val,
                                    }
                                    st.rerun()
                            elif has_pend:
                                st.caption("⏳")
                            elif not is_valid:
                                if st.button("🔍", key=f"mktc_{mkt_key}",
                                             help="복합 교환으로 가능한 조합 탐색"):
                                    with st.spinner("복합 조합 탐색 중..."):
                                        mandatory = [{'target': poster, 'turn': t}]
                                        found = mgr.find_completing_combos(user, mandatory, max_additional=2)
                                    st.session_state.mkt_combos[mkt_key] = found
                                    st.rerun()

                        # 복합 탐색 결과 표시
                        mkt_result = st.session_state.mkt_combos.get(mkt_key)
                        if mkt_result is not None:
                            if not mkt_result:
                                st.info("이 교환을 포함한 복합 조합이 없습니다.")
                            else:
                                mkt_sorted = sorted(mkt_result, key=lambda c: len(c['additional']))
                                st.caption(f"💡 복합으로 가능한 조합 {len(mkt_sorted)}개 (최대 5개 표시)")
                                for mci, mc in enumerate(mkt_sorted[:5]):
                                    mc_swaps = mc['all_swaps']
                                    mc_lines = []
                                    for s in mc_swaps:
                                        tag = "➕ 추가" if s.get('is_additional') else "✓ 이 교환"
                                        mc_lines.append(
                                            f"**{tag}** **{s['turn']}** &nbsp;"
                                            f"나: `{s['my_val']}` ↔ **{s['partner']}**: `{s['partner_val']}`"
                                        )
                                    mc1, mc2 = st.columns([5, 1])
                                    mc1.write("  \n".join(mc_lines))
                                    if mc2.button("⬆️ 신청", key=f"mktcs_{mkt_key}_{mci}",
                                                  use_container_width=True, help="이 조합으로 교환 신청"):
                                        confirm_items = [
                                            {'target': s['partner'], 'turn': s['turn'],
                                             'my_val': s['my_val'], 'partner_val': s['partner_val']}
                                            for s in mc_swaps
                                        ]
                                        st.session_state.multi_confirm = confirm_items
                                        st.rerun()
                            if st.button("✕ 닫기", key=f"mktcc_{mkt_key}"):
                                del st.session_state.mkt_combos[mkt_key]
                                st.rerun()

# ── 장터: 내 턴 올리기 ──────────────────────────────────────────────────────
with tab_post:
    st.write("#### 교환 요청 올리기")
    fv = st.session_state.mkt_form_version
    # 등록 성공 메시지 (폼 리셋 후 표시)
    if st.session_state.mkt_post_success:
        st.success(st.session_state.mkt_post_success)
        st.session_state.mkt_post_success = ''

    all_turns_p = [c for c in mgr.df.columns if c not in LOCKED_TURNS] if not mgr.df.empty else []

    give_mode = st.radio(
        "등록 방식",
        ["📤 줄 턴 지정", "📥 받을 턴 지정"],
        horizontal=True, key=f"mkt_give_mode_{fv}",
        help="줄 턴: 내가 교환해 줄 턴 지정 | 받을 턴: 내가 받고 싶은 턴 지정 (상대에게 아무 턴이나 제안)"
    )

    give_turn_k = ''
    give_val_k  = ''
    want_str_k  = '무관'

    if give_mode == "📤 줄 턴 지정":
        sel_turn_d  = st.selectbox("주고싶은 턴", all_turns_p, key=f"mkt_give_turn_{fv}")
        give_turn_k = sel_turn_d or ''
        if user in mgr.df.index and give_turn_k in mgr.df.columns:
            give_val_k = mgr.df.loc[user, give_turn_k] or ''
            st.write(f"📌 내 {give_turn_k} 현재 값: **`{give_val_k}`**")
            const_p = mgr.get_exchange_constraints(user, give_turn_k)
            if const_p:
                cp1, cp2 = st.columns(2)
                with cp1:
                    if const_p['is_free']:
                        st.success("✅ 어떤 과도 받을 수 있어요")
                    else:
                        st.error(f"🚫 반드시 받아야 할 과: **{', '.join(sorted(const_p['required_to_receive']))}**")
                with cp2:
                    if const_p['already_covered']:
                        st.success(f"✅ 다른 턴 확보: {', '.join(sorted(const_p['already_covered']))}")
        all_vals_want = sorted(set(
            str(v).strip() for col in mgr.df.columns
            for v in mgr.df[col].dropna()
            if v and str(v).strip() not in ('None', '', '예비턴')
        )) if not mgr.df.empty else []
        p_want = st.multiselect("받고싶은 과목 (선택하지 않으면 무관)", all_vals_want,
                                key=f"mkt_want_sel_{fv}")
        want_str_k = ', '.join(p_want) if p_want else '무관'
        can_post = bool(give_turn_k)

    else:  # 받을 턴 지정
        all_vals_want2 = sorted(set(
            str(v).strip() for col in mgr.df.columns
            for v in mgr.df[col].dropna()
            if v and str(v).strip() not in ('None', '', '예비턴')
        )) if not mgr.df.empty else []
        sel_want_vals = st.multiselect(
            "받고 싶은 과목 (복수 선택 가능, 예: IM, ANE)",
            all_vals_want2,
            key=f"mkt_want_vals_{fv}",
            help="선택한 과목 중 하나라도 갖고 있는 사람의 교환 신청을 받을 수 있습니다. 선택하지 않으면 무관"
        )
        give_turn_k = '아무턴'
        give_val_k  = ''
        want_str_k  = ', '.join(sel_want_vals) if sel_want_vals else '무관'
        if sel_want_vals:
            st.info(f"💡 **{', '.join(sel_want_vals)}** 과목이 있는 턴을 줄 수 있는 사람의 교환 신청을 받습니다.")
        else:
            st.caption("과목을 선택하지 않으면 어떤 과목이든 무관하게 등록됩니다.")
        can_post = True  # 과목 미선택(무관)도 등록 가능

    p_msg = st.text_area(
        "메시지 (선택)", placeholder="교환 사유, 연락처 등",
        key=f"mkt_msg_inp_{fv}", max_chars=100
    )
    if st.button("📌 장터에 등록", type="primary", key=f"btn_mkt_post_{fv}",
                 disabled=not can_post):
        ok, msg = mgr.add_market_post(user, give_turn_k, give_val_k, want_str_k, p_msg)
        if ok:
            st.session_state.mkt_post_success = '장터에 등록되었습니다! 🎉'
            st.session_state.mkt_form_version += 1
            st.rerun()
        else:
            st.error(msg)

# ── 장터: 내 게시물 ──────────────────────────────────────────────────────────
with tab_mine:
    if not my_posts:
        st.info("활성 게시물이 없습니다.")
    else:
        for post in my_posts:
            give_t = post.get('주고싶은턴', '')
            give_v = post.get('주고싶은값', '')
            want_  = post.get('받고싶은과', '무관')
            col_mi, col_mc = st.columns([5, 1])
            with col_mi:
                if give_t == '아무턴':
                    t_disp = '아무 턴이나'
                elif give_t:
                    t_disp = give_t
                else:
                    t_disp = '무관'
                st.write(
                    f"**{t_disp}**{(': `'+give_v+'`') if give_v else ''} "
                    f"| 원하는 과목: {want_ or '무관'}"
                    f" | 등록: {post.get('등록시각', '')}"
                )
                if post.get('메시지'):
                    st.caption(f"💬 {post.get('메시지')}")
            with col_mc:
                if st.button("취소", key=f"mine_cancel_{post.get('등록ID')}", type="secondary"):
                    ok, msg = mgr.close_market_post(post.get('등록ID'), '취소')
                    if ok: st.success(msg); st.rerun()
                    else:  st.error(msg)
    done_posts = [p for p in mgr.market_posts
                  if p.get('등록자') == user and p.get('상태') != '활성']
    if done_posts:
        with st.expander(f"종료된 게시물 ({len(done_posts)}건)"):
            for post in done_posts:
                give_t = post.get('주고싶은턴', '')
                give_v = post.get('주고싶은값', '')
                if give_t == '아무턴':
                    t_disp = '아무 턴이나'
                elif give_t:
                    t_disp = give_t
                else:
                    t_disp = '무관'
                st.write(
                    f"[{post.get('상태')}] **{t_disp}**"
                    f"{(': `'+give_v+'`') if give_v else ''}"
                    f" | 원하는 과목: {post.get('받고싶은과','무관')}"
                    f" | {post.get('등록시각','')}"
                )

# ──────────────────────────────────────────────────────────────────────────────
# 📖 사용 설명서
# ──────────────────────────────────────────────────────────────────────────────
st.markdown("---")
with st.expander("📖 사용 설명서 (처음 사용하시나요? 여기를 눌러보세요!)"):
    st.markdown("""
## 🏥 차병원 인턴 턴표 교환 시스템 사용 설명서

> 이 시스템은 인턴들이 서로 편리하게 **턴(근무 일정)을 교환**할 수 있도록 만들어졌어요.
> 모든 교환은 **필수과목(IM, GS, OB, PE, 진로탐색) 규칙**을 자동으로 검증해서
> 규칙을 어기는 교환은 애초에 신청이 불가능하니 안심하세요! 😊

---

### 🔐 1. 로그인

- **이름**: 본인 이름을 입력하세요 (예: 홍길동)
- **비밀번호**: 처음 접속하면 기본 비밀번호는 `1234`예요
- 처음 로그인하면 **비밀번호 변경 화면**이 자동으로 나타납니다. 꼭 변경해주세요!
- 비밀번호는 사이드바 → `🔑 비밀번호 변경` 에서 언제든 바꿀 수 있어요

> 💡 **화면 전환**: 오른쪽 상단에 `📱 모바일 버전으로 전환` / `💻 PC 버전으로 전환` 버튼으로 화면 레이아웃을 바꿀 수 있어요

---

### 📱 2. 화면 구성

로그인 후 화면은 크게 두 부분으로 나뉩니다.

| 영역 | 내용 |
|------|------|
| **메인 화면** | 교환 신청 / 전체 스케줄 / 교환 장터 |
| **사이드바** (왼쪽 `>` 버튼) | 내 현황 / 요청 내역 / 교환 시뮬레이션 |

---

### 🔄 3. 교환 신청하기

가장 기본적인 기능이에요. **내가 원하는 상대방과 특정 턴을 교환**할 수 있어요.

**① 상대방과 턴 선택**
- `상대방`: 교환하고 싶은 상대의 이름을 선택하세요
- `턴`: 교환할 턴(3턴, 4턴 등)을 선택하세요
- 선택하면 **"나: ○○ ↔ 상대방: ○○"** 형태로 교환 내용이 미리 보여요

> ⛔ **1턴, 2턴**은 교환이 불가능한 턴이에요

**② 여러 건 동시 신청 (복합 교환)**
- `➕ 항목 추가` 버튼을 누르면 여러 교환을 한 번에 묶어서 신청할 수 있어요
- 여러 건을 묶으면 **모든 상대방이 수락해야만 교환이 실행**돼요

**③ 조건 확인**
- ✅ 초록색: 교환 가능해요! `📨 요청 보내기` 버튼을 눌러 신청하세요
- ⚠️ 빨간색: 필수과목 규칙 위반 → **`🔍 가능한 조합 찾기`** 를 눌러보세요

**④ 가능한 조합 찾기** (조건 불충족 시)
- 내가 원하는 교환을 포함해서 조건을 충족시키는 추가 교환 조합을 자동으로 탐색해요
- 🔄 **보완** 표시: 상대방이 제3자와 교환해야 조건이 충족되는 경우예요
  → 이 경우 상대방에게 따로 연락해서 함께 조율해야 해요
- `⬆️ 설정` 버튼을 누르면 해당 조합이 교환 신청 칸에 자동으로 채워져요

---

### 📋 4. 전체 스케줄 보기

모든 인턴의 턴표를 한눈에 볼 수 있어요.

- **노란 배경**: 나의 행
- **파란 배경**: 내가 교환 신청한 상대방
- **빨간 테두리**: 현재 교환 신청 중인 턴

---

### 🏪 5. 교환 장터

직접 상대방을 찾기 어려울 때 **게시판 형태**로 교환 요청을 올릴 수 있어요.

#### 🔍 장터 둘러보기
- 다른 인턴들이 올린 교환 요청을 볼 수 있어요
- 🟢 초록 점: 나와 바로 교환 가능한 게시물
- 🔴 빨간 점: 단독으로는 불가 (복합 교환 탐색으로 가능할 수 있어요)
- 가능한 조합이 있으면 `응하기` 버튼이 나타나요

#### 📝 내 턴 올리기
두 가지 방식으로 게시글을 올릴 수 있어요:
- **줄 턴 지정**: "내가 ○턴을 줄게, ○○ 과목을 받고 싶어"
- **받을 턴 지정**: "○○ 과목을 받고 싶어, 내 아무 턴이나 줄게"
- 받고 싶은 과목, 메시지도 함께 적을 수 있어요

#### 🗂️ 내 게시물
- 내가 올린 활성 게시물을 관리할 수 있어요
- `취소` 버튼으로 게시물을 내릴 수 있어요
- 교환이 완료되면 자동으로 마감 처리돼요

---

### 📩 6. 교환 요청 받았을 때 (사이드바)

왼쪽 사이드바를 열면 **받은 요청**을 확인할 수 있어요.

- `✅ 수락`: 교환이 즉시 실행돼요 (구글 시트에도 자동 반영!)
- `❌ 거절`: 요청을 거절해요
- 복합 교환(여러 건 묶음)은 **모든 상대방이 수락해야** 실행돼요

> 💡 새로운 요청이 있으면 사이드바 상단에 숫자로 표시돼요

---

### 🧪 7. 교환 시뮬레이션 (사이드바 하단)

교환 신청 전에 **어떤 교환이 가능한지 미리 탐색**할 수 있어요.

| 모드 | 설명 |
|------|------|
| 🔄 **특정 턴 교환** | 특정 턴을 기준으로 교환 가능한 파트너 전체 목록을 보여줘요 |
| 🎯 **특정 턴 받기** | 내가 받고 싶은 과목(복수 선택 가능!)을 얻을 수 있는 모든 (파트너, 턴) 조합을 탐색해요 |
| 🔗 **복합 교환 탐색** | 2~3개 턴을 동시에 교환해야만 가능한 조합을 찾아줘요 |

- `요청` 버튼을 누르면 바로 교환 신청 화면으로 연결돼요
- ⏳ 표시: 이미 대기 중인 요청이 있어요

---

### ⚠️ 알아두세요

| 항목 | 내용 |
|------|------|
| 필수과목 | IM, GS, OB, PE, 진로탐색 — 교환 후에도 5개 과목이 모두 있어야 해요 |
| 교환 불가 턴 | 1턴, 2턴은 교환할 수 없어요 |
| 실시간 반영 | 교환 완료 시 구글 시트에 자동으로 반영돼요 |
| 새로고침 | 다른 사람이 교환한 내용을 보려면 `🔄 새로고침` 버튼을 눌러주세요 |
| 비밀번호 | 기본 비밀번호 `1234`는 꼭 변경해주세요! |
""")

