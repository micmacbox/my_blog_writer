"""Independent Weekly Food web entry point: streamlit run app.py."""
import streamlit as st
from weekly_food.auth import require_password
from weekly_food.ui import render_weekly_food

st.set_page_config(page_title="Weekly Food · 주간 푸드 트렌드", page_icon="🍽️", layout="wide")
require_password()
render_weekly_food()
