import { Box } from "@mui/material";
import CalendarTodayIcon from "@mui/icons-material/CalendarToday";

// A calendar glyph carrying the number of days a range preset covers (`1` = today, `7` = this week).
export function CalendarDaysIcon({ days }: { days: number }) {
    return (
        <Box sx={{ position: "relative", display: "inline-flex" }}>
            <CalendarTodayIcon fontSize="small" />
            <Box
                component="span"
                sx={{
                    position: "absolute",
                    inset: 0,
                    display: "flex",
                    alignItems: "flex-end",
                    justifyContent: "center",
                    pb: "14%",
                    fontSize: 9,
                    fontWeight: 700,
                    lineHeight: 1,
                }}
            >
                {days}
            </Box>
        </Box>
    );
}
