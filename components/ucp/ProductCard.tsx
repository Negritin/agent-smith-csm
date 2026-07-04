'use client';

import { useState, useEffect } from 'react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { ShoppingCart, Eye, Package } from 'lucide-react';
import { cn } from '@/lib/utils';

interface UCPProduct {
  id: string;
  title: string;
  description?: string;
  handle?: string;
  available: boolean;
  price: { amount: string; currency: string };
  image_url?: string;
  image_alt?: string;
  images?: Array<{ url: string; alt?: string }>;
  variants: any[];
  options?: any[];
  has_variants?: boolean;
}

interface ProductCardProps {
  product: UCPProduct;
  size?: 'default' | 'large';
  shopDomain?: string;
  onSendMessage?: (message: string) => void;
}

function formatPrice(price: { amount: string; currency: string }): string {
  const amount = parseFloat(price.amount);
  return new Intl.NumberFormat('pt-BR', {
    style: 'currency',
    currency: price.currency || 'BRL',
  }).format(amount);
}

export function ProductCard({
  product,
  size = 'default',
  shopDomain,
  onSendMessage,
}: ProductCardProps) {
  const [imageError, setImageError] = useState(false);
  const [isHovered, setIsHovered] = useState(false);
  const [selectedOptions, setSelectedOptions] = useState<Record<string, string>>({});

  // Initialize selected options with defaults
  useEffect(() => {
    if (
      product.options &&
      product.options.length > 0 &&
      Object.keys(selectedOptions).length === 0
    ) {
      const defaults: Record<string, string> = {};
      product.options.forEach((opt: any) => {
        if (opt.values && opt.values.length > 0) {
          defaults[opt.name] = opt.values[0];
        }
      });
      setSelectedOptions(defaults);
    }
  }, [product, selectedOptions]);

  const getSelectedVariant = () => {
    if (!product.variants || product.variants.length === 0) return null;
    if (!product.options || product.options.length === 0) return product.variants[0];

    return (
      product.variants.find((variant: any) => {
        // Check matches against variant.selectedOptions if available
        if (variant.selectedOptions) {
          return product.options?.every((opt: any) => {
            const selectedValue = selectedOptions[opt.name];
            const variantOption = variant.selectedOptions.find((o: any) => o.name === opt.name);
            return variantOption?.value === selectedValue;
          });
        }
        // Fallback: check against option1, option2, etc. (Shopify structure)
        return product.options?.every((opt: any, index: number) => {
          const selectedValue = selectedOptions[opt.name];
          const optionKey = `option${index + 1}`;
          return variant[optionKey] === selectedValue;
        });
      }) || product.variants[0]
    );
  };

  const selectedVariant = getSelectedVariant();

  // Computed properties
  const currentPrice = selectedVariant?.price ? selectedVariant.price : product.price;
  const isLarge = size === 'large';
  // Prefer variant image, then product main image
  const imageUrl = selectedVariant?.image?.url || product.image_url || product.images?.[0]?.url;
  const isAvailable = selectedVariant ? selectedVariant.available : product.available !== false;
  const hasVariants = product.has_variants || (product.variants && product.variants.length > 1);

  const handleOptionChange = (optionName: string, value: string) => {
    setSelectedOptions((prev) => ({
      ...prev,
      [optionName]: value,
    }));
  };

  const handleViewMore = () => onSendMessage?.(`Me conta mais sobre "${product.title}"`);

  const handleBuy = () => {
    // Determine the checkout URL for the specific selected variant
    let finalCheckoutUrl = '';

    // Try to extract domain from existing checkout_url if shopDomain prop is missing
    let domain = shopDomain;
    if (!domain && (product as any).checkout_url) {
      try {
        const urlObj = new URL((product as any).checkout_url);
        domain = urlObj.host;
      } catch (e) {}
    }

    if (selectedVariant && selectedVariant.id && domain) {
      const variantId = String(selectedVariant.id).split('/').pop();
      finalCheckoutUrl = `https://${domain}/cart/${variantId}:1`;
    } else {
      finalCheckoutUrl = (product as any).checkout_url;
    }

    if (finalCheckoutUrl) {
      const width = 500;
      const height = 700;
      const left = window.screenX + (window.outerWidth - width) / 2;
      const top = window.screenY + (window.outerHeight - height) / 2;

      window.open(
        finalCheckoutUrl,
        'shopify-checkout',
        `width=${width},height=${height},left=${left},top=${top},resizable=yes,scrollbars=yes`,
      );
      return;
    }

    // Fallback: message agent
    const variantId = selectedVariant?.id || (product as any).variant_id;
    const variantTitle =
      selectedVariant?.title !== 'Default Title' ? ` (${selectedVariant?.title})` : '';

    if (variantId) {
      onSendMessage?.(`Quero comprar "${product.title}"${variantTitle} (Variant ID: ${variantId})`);
    } else {
      onSendMessage?.(`Quero comprar "${product.title}"`);
    }
  };

  return (
    <div
      className={cn(
        'group relative flex flex-col overflow-hidden rounded-xl',
        'bg-card border border-border transition-all duration-300',
        'hover:border-border hover:shadow-lg',
        // Width slightly reduced to compensate for extreme height of Reels ratio, keeping it manageable
        isLarge ? 'w-[220px]' : 'w-[170px]',
      )}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
    >
      {/* Image - Portrait Aspect Ratio (3:4) */}
      <div
        className={cn(
          'relative overflow-hidden bg-muted aspect-[3/4]',
          isLarge ? 'h-[293px]' : 'h-[226px]',
        )}
      >
        {imageUrl && !imageError ? (
          <img
            src={imageUrl}
            alt={product.image_alt || product.title}
            className={cn(
              'w-full h-full object-cover transition-transform duration-500',
              isHovered && 'scale-105',
            )}
            onError={() => setImageError(true)}
            loading="lazy"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center">
            <Package className="h-12 w-12 text-muted-foreground" />
          </div>
        )}

        {/* Hover overlay */}
        <div
          className={cn(
            'absolute inset-0 bg-foreground/35 flex items-center justify-center gap-2',
            'opacity-0 transition-opacity duration-300',
            isHovered && 'opacity-100',
          )}
        >
          <Button
            size="sm"
            variant="secondary"
            className="h-9 bg-card/90 text-foreground hover:bg-card"
            onClick={handleViewMore}
          >
            <Eye className="h-4 w-4 mr-1.5" />
            Detalhes
          </Button>
        </div>

        {!isAvailable && (
          <div className="absolute top-2 left-2">
            <Badge variant="destructive" className="text-[10px] px-2 py-0.5">
              Indisponível
            </Badge>
          </div>
        )}
      </div>

      {/* Content */}
      <div className="flex flex-col flex-1 p-4 space-y-3">
        <div>
          <h4
            className={cn(
              'font-medium text-foreground line-clamp-2',
              isLarge ? 'text-base' : 'text-sm',
            )}
            title={product.title}
          >
            {product.title}
          </h4>
        </div>

        {/* Variant Selectors */}
        {hasVariants && product.options && product.options.length > 0 ? (
          <div className="space-y-2">
            {product.options.map((opt: any) => (
              <div key={opt.name} className="space-y-1">
                <span className="text-[10px] uppercase tracking-wider text-muted-foreground font-bold">
                  {opt.name}
                </span>
                <div className="flex flex-wrap gap-1.5">
                  {opt.values.map((val: string) => {
                    const isSelected = selectedOptions[opt.name] === val;
                    return (
                      <button
                        key={val}
                        onClick={() => handleOptionChange(opt.name, val)}
                        className={cn(
                          'px-2 py-1 text-[10px] rounded border transition-colors',
                          isSelected
                            ? 'bg-brand-muted text-primary border-primary/30 font-semibold'
                            : 'bg-transparent text-muted-foreground border-border hover:border-primary/30',
                        )}
                      >
                        {val}
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        ) : (
          /* Placeholder spacer if no variants, to align buttons */
          <div className="flex-1" />
        )}

        <div className="pt-2 space-y-3 border-t border-border/50 mt-auto">
          <div className="flex items-center justify-between">
            <span className={cn('font-bold text-primary', isLarge ? 'text-lg' : 'text-base')}>
              {formatPrice(currentPrice)}
            </span>
          </div>

          <Button
            size="sm"
            className="w-full h-9 text-xs font-bold bg-card text-foreground hover:bg-muted disabled:bg-muted disabled:text-muted-foreground"
            disabled={!isAvailable}
            onClick={handleBuy}
          >
            <ShoppingCart className="h-3.5 w-3.5 mr-1.5" />
            {isAvailable ? 'Comprar Agora' : 'Indisponível'}
          </Button>
        </div>
      </div>
    </div>
  );
}
