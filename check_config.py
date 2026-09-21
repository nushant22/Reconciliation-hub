"""Quick script to check Streamlit's actual maxUploadSize configuration."""
import streamlit as st
from streamlit import config

st.title("Config Check")
st.write("Current maxUploadSize setting:")

try:
    max_size = config.get_option("server.maxUploadSize")
    st.success(f"maxUploadSize = {max_size} MB")
except Exception as e:
    st.error(f"Error reading config: {e}")

st.write("---")
st.write("File uploader test:")
uploaded = st.file_uploader("Test upload", type=["csv", "xlsx"])
if uploaded:
    st.info(f"File size: {len(uploaded.getvalue()) / 1024 / 1024:.2f} MB")
