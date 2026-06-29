# app.py
import streamlit as st
import pandas as pd
import tempfile
import os
import zipfile
from io import BytesIO
from pathlib import Path
from parser_engine import run_full_pipeline

st.set_page_col_config = st.set_page_config(
    page_title="Valheim Rewind Save Parser",
    page_icon="🛡️",
    layout="wide"
)

# Compact layout styling
st.markdown("""
<style>
    .block-container {padding-top: 2rem !important; padding-bottom: 0rem !important;}
    div[data-testid="stAlert"] {padding: 0.1rem 0.1rem !important; margin-bottom: 0rem !important; font-size: 0.8em !important; min-height: 0 !important;}
    div[data-testid="stAlert"] p {margin: 0 !important;}
    h1, h2, h3 {margin-top: 0.1rem !important; margin-bottom: 0.1rem !important;}
    div[data-testid="stVerticalBlock"] > div {gap: 0rem !important; margin-bottom: 0rem !important;}
    .stDownloadButton, .stDownloadButton > div {margin-top: 0 !important; padding-top: 0 !important;}
</style>
""", unsafe_allow_html=True)

st.subheader("🛡️ Valheim Rewind Save File Parser")
st.caption("Upload a `Rewind` to show items in containers, loot from breakable prefabs, and enemy drops.")

# Two-column layout: upload left, status right
col_upload, col_status = st.columns([1, 1])

with col_upload:
    uploaded_file = st.file_uploader("Choose a save file")

def build_zip(files_dict):
    """Zips the output CSVs into an in-memory buffer."""
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
        for label, file_path in files_dict.items():
            if os.path.exists(file_path):
                zip_file.write(file_path, arcname=os.path.basename(file_path))
    return zip_buffer.getvalue()

if uploaded_file is not None:
    # Execute extraction pipeline in a temporary folder
    with tempfile.TemporaryDirectory() as temp_workspace:
        # Write uploaded file streams to disk temporarily so mmap/C-regex libraries can read it
        temp_rewind_path = os.path.join(temp_workspace, uploaded_file.name)
        with open(temp_rewind_path, "wb") as f:
            f.write(uploaded_file.getbuffer())
            
        with st.spinner("Processing savefile... This may take up to a minute depending on file size."):
            try:
                outputs = run_full_pipeline(
                    rewind_file_path=temp_rewind_path,
                    output_dir=temp_workspace,
                    prefabs_csv="prefabs.csv",
                    hexpat_file="rewind.hexpat",
                    itemlist_csv="itemlist.csv",
                    breakables_loot_csv="breakablesLoot.csv",
                    creature_loot_csv="creatureLoot.csv"
                )
                st.balloons()
                with col_status:
                    st.success("✅ Parsing complete")
                    # Bundle everything into a neat master zip
                    zip_data = build_zip(outputs)
                    st.download_button(
                        label="📦 Download All Tables (ZIP)",
                        data=zip_data,
                        file_name=f"parsed_loot_{Path(uploaded_file.name).stem}.zip",
                        mime="application/zip",
                        use_container_width=True
                    )
                
                # Display output files in tabs
                tab1, tab2, tab3 = st.tabs([
                    "📦 Condensed Loot Items", 
                    "🏺 Breakables Loot Averages", 
                    "👾 Creature Loot Averages"
                ])
                
                with tab1:
                    st.write("Accumulated items from chests, itemstands, and armor stands.")
                    file_p = outputs["Condensed Items (Processed)"]
                    if os.path.exists(file_p):
                        df = pd.read_csv(file_p)
                        st.dataframe(df, use_container_width=True, height=600)

                        # Direct individual file download button
                        with open(file_p, "rb") as df_file:
                            st.download_button(
                                label="📥 Download Condensed Items CSV",
                                data=df_file,
                                file_name=os.path.basename(file_p),
                                mime="text/csv"
                            )

                with tab2:
                    st.write("Estimated drops based on split drop-rates for built/unbuilt prefabs.")
                    file_p = outputs["Breakables Loot (Processed)"]
                    if os.path.exists(file_p):
                        df = pd.read_csv(file_p)
                        st.dataframe(df, use_container_width=True, height=600)

                        with open(file_p, "rb") as df_file:
                            st.download_button(
                                label="📥 Download Breakables Loot CSV",
                                data=df_file,
                                file_name=os.path.basename(file_p),
                                mime="text/csv"
                            )

                with tab3:
                    st.write("Aggregated drops from all level-scaled creature instances.")
                    file_p = outputs["Creatures Loot (Processed)"]
                    if os.path.exists(file_p):
                        df = pd.read_csv(file_p)
                        st.dataframe(df, use_container_width=True, height=600)

                        with open(file_p, "rb") as df_file:
                            st.download_button(
                                label="📥 Download Creature Loot CSV",
                                data=df_file,
                                file_name=os.path.basename(file_p),
                                mime="text/csv"
                            )
                
                # Expandable developer tools section for raw outputs
                with st.expander("🛠️ Advanced Raw Tables (Pre-Aggregation)"):
                    st.write("Raw item stacks and coordinate matrices:")
                    
                    raw_tabs = st.tabs(["Raw Container Elements", "Raw Breakables Coordinates", "Raw Creatures Levels"])
                    
                    with raw_tabs[0]:
                        raw_items_p = outputs["Items Table (Raw)"]
                        if os.path.exists(raw_items_p):
                            st.dataframe(pd.read_csv(raw_items_p), use_container_width=True, height=1200)
                    with raw_tabs[1]:
                        raw_breaks_p = outputs["Breakables Prefabs (Raw)"]
                        if os.path.exists(raw_breaks_p):
                            st.dataframe(pd.read_csv(raw_breaks_p), use_container_width=True, height=1200)
                    with raw_tabs[2]:
                        raw_creats_p = outputs["Creatures Prefabs (Raw)"]
                        if os.path.exists(raw_creats_p):
                            st.dataframe(pd.read_csv(raw_creats_p), use_container_width=True, height=1200)
                            
            except Exception as ex:
                with col_status:
                    st.error(f"Failed to parse file.")
                    st.exception(ex)