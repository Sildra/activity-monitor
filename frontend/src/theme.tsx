import { createTheme, CssBaseline, ThemeProvider, useMediaQuery } from "@mui/material";
import { useMemo } from "react";
import type { ReactNode } from "react";

export default function AppTheme({ children }: { children: ReactNode }) {
    const prefersDarkMode = useMediaQuery("(prefers-color-scheme: dark)");

    const theme = useMemo(
        () =>
            createTheme({
                palette: {
                    mode: prefersDarkMode ? "dark" : "light",
                    primary: prefersDarkMode ? { main: "#7dd3fc" } : { main: "#0284c7" },
                },
                components: {
                    MuiCssBaseline: {
                        styleOverrides: {
                            ":root": {
                                colorScheme: prefersDarkMode ? "dark" : "light",
                            },
                            "*::-webkit-scrollbar": {
                                width: 10,
                                height: 10,
                            },
                            "*::-webkit-scrollbar-thumb": {
                                backgroundColor: prefersDarkMode
                                    ? "rgba(255,255,255,0.35)"
                                    : "rgba(0,0,0,0.35)",
                                borderRadius: 8,
                                border: "2px solid transparent",
                                backgroundClip: "content-box",
                            },
                            "*::-webkit-scrollbar-track": {
                                backgroundColor: prefersDarkMode
                                    ? "rgba(255,255,255,0.06)"
                                    : "rgba(0,0,0,0.05)",
                            },
                        },
                    },
                },
            }),
        [prefersDarkMode],
    );

    return (
        <ThemeProvider theme={theme}>
            <CssBaseline />
            {children}
        </ThemeProvider>
    );
}
