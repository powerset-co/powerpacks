import { useState, useEffect, useRef } from "react"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import { X, Check } from "lucide-react"
import { cn } from "@/lib/utils"

export interface EntityValue {
  id: string
  display_value: string
  type?: string
}

interface Suggestion {
  id: string
  display_value: string
  type?: string
}

interface AutocompleteInputProps {
  placeholder: string
  value: EntityValue[]
  onChange: (values: EntityValue[]) => void
  onSearch: (query: string) => Promise<Suggestion[]>
  onFetchPreview?: () => Promise<Suggestion[]> // Fetch preview items when focused with no input
  className?: string
  maxDisplayed?: number
  id?: string
}

export const AutocompleteInput = ({
  placeholder,
  value,
  onChange,
  onSearch,
  onFetchPreview,
  className,
  maxDisplayed,
  id,
}: AutocompleteInputProps) => {
  const [inputValue, setInputValue] = useState("")
  const [suggestions, setSuggestions] = useState<Suggestion[]>([])
  const [previewSuggestions, setPreviewSuggestions] = useState<Suggestion[]>([])
  const [showSuggestions, setShowSuggestions] = useState(false)
  const [isLoading, setIsLoading] = useState(false)
  const [isFocused, setIsFocused] = useState(false)
  const [hasFetchedPreview, setHasFetchedPreview] = useState(false)
  const wrapperRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const searchAbortController = useRef<AbortController | null>(null)
  const onSearchRef = useRef(onSearch)
  const onFetchPreviewRef = useRef(onFetchPreview)

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(event.target as Node)) {
        setShowSuggestions(false)
      }
    }

    document.addEventListener("mousedown", handleClickOutside)
    return () => document.removeEventListener("mousedown", handleClickOutside)
  }, [])

  useEffect(() => {
    onSearchRef.current = onSearch
  }, [onSearch])

  useEffect(() => {
    onFetchPreviewRef.current = onFetchPreview
  }, [onFetchPreview])

  useEffect(() => {
    const searchSuggestions = async () => {
      const trimmed = inputValue.trim()

      // For empty input, show preview if available
      if (trimmed.length === 0) {
        setSuggestions([])
        setIsLoading(false)
        // Keep showing dropdown if we have preview suggestions
        if (isFocused && previewSuggestions.length > 0) {
          setShowSuggestions(true)
        } else {
          setShowSuggestions(false)
        }
        return
      }

      // Don't search for very short input
      if (trimmed.length < 2) {
        setSuggestions([])
        setIsLoading(false)
        return
      }

      // Abort any previous search in progress
      if (searchAbortController.current) {
        searchAbortController.current.abort()
      }
      searchAbortController.current = new AbortController()

      // Show dropdown and loading state immediately
      // Keep showing previous suggestions while loading new ones
      setShowSuggestions(true)
      setIsLoading(true)

      try {
        const results = await onSearchRef.current(trimmed)

        // Only update if this search wasn't aborted
        if (!searchAbortController.current?.signal.aborted) {
          setSuggestions(results)
          setShowSuggestions(true)
        }
      } catch (error) {
        if (error instanceof Error && error.name !== "AbortError") {
          console.error("Autocomplete error:", error)
          setSuggestions([])
        }
      } finally {
        if (!searchAbortController.current?.signal.aborted) {
          setIsLoading(false)
        }
      }
    }

    const debounceTimer = setTimeout(searchSuggestions, 300)
    return () => {
      clearTimeout(debounceTimer)
      // Abort search if component unmounts or input changes
      if (searchAbortController.current) {
        searchAbortController.current.abort()
      }
    }
  }, [inputValue])

  const handleSelectSuggestion = (suggestion: Suggestion) => {
    const isSelected = value.some((v) => v.id === suggestion.id)

    if (isSelected) {
      // Deselect if already selected
      onChange(value.filter((v) => v.id !== suggestion.id))
    } else {
      // Add to selection
      onChange([
        ...value,
        {
          id: suggestion.id,
          display_value: suggestion.display_value,
          type: suggestion.type,
        },
      ])
    }

    // Don't clear input or hide suggestions - allow multiple selections
    // User can press Escape or click outside to close
  }

  const handleRemove = (item: EntityValue) => {
    onChange(value.filter((v) => v.id !== item.id))
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      setShowSuggestions(false)
      setInputValue("")
      setSuggestions([])
    } else if (e.key === "Enter" && inputValue.trim() && suggestions.length === 0 && !isLoading) {
      e.preventDefault()
      const manualEntry = { id: inputValue.trim(), display_value: inputValue.trim() }
      if (!value.some((v) => v.display_value === inputValue.trim())) {
        onChange([...value, manualEntry])
      }
      setInputValue("")
      setSuggestions([])
      setShowSuggestions(false)
    }
  }

  const handleInputFocus = async () => {
    setIsFocused(true)

    // If no input and we have a preview fetcher, fetch preview items
    if (inputValue.trim().length === 0 && onFetchPreviewRef.current && !hasFetchedPreview) {
      setIsLoading(true)
      setShowSuggestions(true)
      try {
        const previewItems = await onFetchPreviewRef.current()
        setPreviewSuggestions(previewItems)
        setHasFetchedPreview(true)
      } catch (error) {
        console.error("Failed to fetch preview:", error)
      } finally {
        setIsLoading(false)
      }
    } else if (inputValue.trim().length === 0 && previewSuggestions.length > 0) {
      // Show preview suggestions if already fetched
      setShowSuggestions(true)
    } else if (suggestions.length > 0 || inputValue.trim().length >= 2) {
      // Show dropdown if we have suggestions or if we're in a searchable state
      setShowSuggestions(true)
    }
  }

  const handleInputBlur = () => {
    // Delay to allow click on dropdown to register
    setTimeout(() => {
      setIsFocused(false)
      // Only hide if not clicking within wrapper
      if (!wrapperRef.current?.contains(document.activeElement)) {
        setShowSuggestions(false)
      }
    }, 150)
  }

  // Determine which suggestions to display (search results or preview)
  const displaySuggestions = inputValue.trim().length === 0 ? previewSuggestions : suggestions

  // Determine what to show in the dropdown - only show when input is focused. Without an entity
  // service a search finds nothing, and Enter keeps the typed name, so no "No results" box.
  const showDropdown = isFocused && showSuggestions && (isLoading || displaySuggestions.length > 0)

  return (
    <div ref={wrapperRef} className={cn("relative", className)}>
      <div className="space-y-2">
        <Input
          ref={inputRef}
          id={id}
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          onFocus={handleInputFocus}
          onBlur={handleInputBlur}
        />

        {value.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {(maxDisplayed ? value.slice(0, maxDisplayed) : value).map((item) => (
              <Badge key={item.id} variant="secondary" className="pl-3 pr-1">
                {item.display_value}
                <button onClick={() => handleRemove(item)} className="ml-1 hover:bg-muted rounded-full p-0.5">
                  <X className="h-3 w-3" />
                </button>
              </Badge>
            ))}
          </div>
        )}
      </div>

      {showDropdown && (
        <div className="absolute z-50 w-full mt-1 bg-popover border rounded-md shadow-lg max-h-72 overflow-auto">
          {isLoading ? (
            <div className="p-3 text-sm text-muted-foreground">Loading...</div>
          ) : (
            displaySuggestions.map((suggestion) => {
              const isSelected = value.some((v) => v.id === suggestion.id)
              return (
                <button
                  key={suggestion.id}
                  onClick={() => handleSelectSuggestion(suggestion)}
                  className={cn(
                    "w-full text-left px-3 py-2 hover:bg-accent text-sm transition-colors flex items-center justify-between",
                    isSelected && "bg-accent/50",
                  )}
                >
                  <span>{suggestion.display_value}</span>
                  {isSelected && <Check className="h-4 w-4 text-primary" />}
                </button>
              )
            })
          )}
        </div>
      )}
    </div>
  )
}
