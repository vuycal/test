import pandas as pd
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

# 1. Load the two Excel files
df_facility = pd.read_excel("MPID_Facility_List.xlsx")
df_list = pd.read_excel("MPID_list.xlsx")

# Clean any stray unnamed columns in df_list if present
df_list = df_list.loc[:, ~df_list.columns.str.contains('^Unnamed')]

# Strip whitespace from MPID strings to ensure accurate matching
df_facility['MPID'] = df_facility['MPID'].astype(str).str.strip()
df_list['MPID'] = df_list['MPID'].astype(str).str.strip()

# Create a mapping dictionary for MPID -> Facility Name
facility_map = df_facility.dropna(subset=['Facility Name']).drop_duplicates(subset=['MPID']).set_index('MPID')['Facility Name'].to_dict()

# 2. Merge and map Facility Name into df_list
df_merged = df_list.copy()
df_merged['Facility Name'] = df_merged['MPID'].map(facility_map)

# For generators/MPIDs without a matching facility name, put the MPID itself
df_merged['Facility Name'] = df_merged['Facility Name'].fillna(df_merged['MPID'])

# Reorder columns so Facility Name sits next to MPID
cols = ['MPID', 'Facility Name'] + [col for col in df_merged.columns if col not in ['MPID', 'Facility Name']]
df_merged = df_merged[cols]

# 3. Save to Excel and apply professional styling using openpyxl
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Merged MPID Data"

# Define styles
header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
data_font = Font(name="Calibri", size=11, bold=False, color="000000")
thin_border = Border(
    left=Side(style='thin', color='D9D9D9'),
    right=Side(style='thin', color='D9D9D9'),
    top=Side(style='thin', color='D9D9D9'),
    bottom=Side(style='thin', color='D9D9D9')
)

# Write headers and data rows
ws.append(list(df_merged.columns))
for row in df_merged.itertuples(index=False):
    ws.append(list(row))

# Apply formatting across all cells
for r_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=ws.max_row, min_col=1, max_col=ws.max_column), start=1):
    for cell in row:
        if r_idx == 1:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="left", vertical="center")
        else:
            cell.font = data_font
            cell.alignment = Alignment(horizontal="left", vertical="center")
            cell.border = thin_border

# Auto-adjust column widths for readability
for col in ws.columns:
    max_len = max(len(str(cell.value or '')) for cell in col)
    col_letter = openpyxl.utils.get_column_letter(col[0].column)
    ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

# Save the final workbook
output_filename = "Merged_MPID_Facility_List.xlsx"
wb.save(output_filename)
print(f"Successfully generated merged file: {output_filename}")
