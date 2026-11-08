"""Independent password gate for Weekly Food."""
import hashlib
import hmac
import os

import streamlit as st


def require_password():
    password = os.environ.get('WEEKLY_FOOD_PASSWORD', '')
    if not password:
        try:
            password = st.secrets.get('WEEKLY_FOOD_PASSWORD', '')
        except FileNotFoundError:
            password = ''
    if not isinstance(password, str) or not password.strip():
        st.error('관리자의 비밀번호 설정이 필요합니다.')
        st.caption('앱 비밀번호가 설정되지 않아 접근이 잠겨 있습니다.')
        st.stop()

    # Recheck configuration on every rerun so password rotation revokes sessions.
    signature = hashlib.sha256(password.encode('utf-8')).hexdigest()
    if hmac.compare_digest(st.session_state.get('_weekly_login_signature', ''), signature):
        def logout():
            st.session_state.clear()

        st.sidebar.button('로그아웃', on_click=logout)
        return

    def verify():
        entered = st.session_state.pop('_weekly_login_password', '')
        if hmac.compare_digest(entered.encode('utf-8'), password.encode('utf-8')):
            st.session_state['_weekly_login_signature'] = signature
            st.session_state.pop('_weekly_login_error', None)
        else:
            st.session_state.pop('_weekly_login_signature', None)
            st.session_state['_weekly_login_error'] = True

    st.title('🔒 Weekly Food')
    with st.form('weekly_login_form', clear_on_submit=True):
        st.text_input('비밀번호', type='password', key='_weekly_login_password')
        st.form_submit_button('로그인', on_click=verify)
    if st.session_state.get('_weekly_login_error'):
        st.error('비밀번호가 올바르지 않습니다.')
    st.stop()
