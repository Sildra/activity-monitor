import { useState } from "react";
import { AppBar, Box, Tab, Tabs, Toolbar, Typography } from "@mui/material";
import TimelinePage from "./pages/TimelinePage";
import TaskPage from "./pages/TaskPage";
import SourcesPage from "./pages/SourcesPage";

type Page = "timeline" | "task" | "sources";

const PAGES: Array<{ id: Page; label: string }> = [
    { id: "timeline", label: "Timeline" },
    { id: "task", label: "Task" },
    { id: "sources", label: "Sources" },
];

function initialPage(): Page {
    const value = new URLSearchParams(window.location.search).get("page");

    return PAGES.some(({ id }) => id === value) ? (value as Page) : "timeline";
}

export default function App() {
    const [page, setPage] = useState<Page>(initialPage);
    const pageIndex = PAGES.findIndex(({ id }) => id === page);

    return (
        <Box sx={{ height: "100svh", display: "flex", flexDirection: "column", overflow: "hidden" }}>
            <AppBar position="static" color="default" elevation={0} sx={{ borderBottom: 1, borderColor: "divider" }}>
                <Toolbar variant="dense" disableGutters sx={{ px: 1, gap: 2 }}>
                    <Typography variant="subtitle1" sx={{ fontWeight: 600, ml: 1 }}>
                        Activity Monitor
                    </Typography>
                    <Tabs
                        value={pageIndex}
                        onChange={(_event, value) => setPage(PAGES[value as number].id)}
                    >
                        {PAGES.map(({ id, label }) => (
                            <Tab key={id} label={label} />
                        ))}
                    </Tabs>
                </Toolbar>
            </AppBar>
            <Box sx={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
                {page === "timeline" && <TimelinePage />}
                {page === "task" && <TaskPage />}
                {page === "sources" && <SourcesPage />}
            </Box>
        </Box>
    );
}
