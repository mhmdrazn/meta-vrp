import { ThemeProvider as NextThemes, type ThemeProviderProps } from 'next-themes'

export function ThemeProvider({ children, ...props }: ThemeProviderProps) {
  return (
    <NextThemes attribute='class' defaultTheme='system' enableSystem {...props}>
      {children}
    </NextThemes>
  )
}
