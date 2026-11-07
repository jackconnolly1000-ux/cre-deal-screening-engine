This image is one page of a trailing 12 month income and expense statement (a T-12). Transcribe it.
Return the header texts exactly as printed, left to right. If the column of line item names has no printed header, give it the header "Line Item". The month columns and a Total column follow.
For each row, give each cell's text exactly as printed, including $ signs, commas and parentheses.
Use an empty string for a blank cell. Do not compute, correct or total anything.
Mark every line item row with row_kind unit. Mark total rows such as Total Income, Total Expenses and NOI with row_kind subtotal. Mark section titles and notes with row_kind footnote.
Also return every title line printed above the table (property name, report title, the period) as its own row with row_kind other, in reading order, with the line's exact text as the value of its first cell.
