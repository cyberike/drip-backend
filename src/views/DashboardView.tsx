import React from 'react';
import { ExtensionContextValue } from '@stripe/ui-extension-sdk/context';
import {
  Badge,
  Box,
  Button,
  ContextView,
  Divider,
  Icon,
  Link,
  List,
  ListItem,
  Spinner,
} from '@stripe/ui-extension-sdk/ui';

const DRIP_API = 'https://dripfinancial.org';

interface Stats {
  total_donated: number;
  transactions_today: number;
  active_charities: number;
  total_transactions: number;
  donation_by_category: { category: string; donated: number }[];
}

interface Settings {
  donation_pct: number;
  auto_donate: boolean;
}

const DashboardView = ({
  userContext,
  environment,
}: ExtensionContextValue) => {
  const [stats, setStats] = React.useState<Stats | null>(null);
  const [settings, setSettings] = React.useState<Settings | null>(null);
  const [loading, setLoading] = React.useState(true);

  React.useEffect(() => {
    const acct = environment?.objectContext?.id;
    const headers: Record<string, string> = {};
    if (acct) headers['Stripe-Account'] = acct;

    const fetchData = async () => {
      try {
        const [statsRes, settingsRes] = await Promise.all([
          fetch(`${DRIP_API}/api/stats`, { headers }),
          fetch(`${DRIP_API}/api/settings`, { headers }),
        ]);

        if (statsRes.ok) setStats(await statsRes.json());
        if (settingsRes.ok) setSettings(await settingsRes.json());
      } catch (err) {
        console.error('Failed to load dashboard:', err);
      } finally {
        setLoading(false);
      }
    };
    fetchData();
  }, [environment]);

  if (loading) {
    return (
      <ContextView title="Drip Donations">
        <Box
          css={{
            padding: 'xlarge',
            layout: 'column',
            alignX: 'center',
            alignY: 'center',
          }}
        >
          <Spinner size="large" />
          <Box css={{ font: 'caption', color: 'secondary', marginTop: 'small' }}>
            Loading your donation dashboard...
          </Box>
        </Box>
      </ContextView>
    );
  }

  const totalDonated = stats?.total_donated ?? 0;
  const txToday = stats?.transactions_today ?? 0;
  const totalTx = stats?.total_transactions ?? 0;
  const activeCharities = stats?.active_charities ?? 0;
  const donationPct = settings?.donation_pct ?? 0;
  const autoDonate = settings?.auto_donate ?? false;

  return (
    <ContextView
      title="Drip Donations"
      description="Automatic charitable giving from every payment"
      externalLink={{
        label: 'Full Dashboard',
        href: `${DRIP_API}/dashboard`,
      }}
    >
      <Box css={{ stack: 'y', gap: 'medium', padding: 'medium' }}>
        {/* Status Badge */}
        <Box css={{ layout: 'row', alignX: 'spread' }}>
          <Badge type={autoDonate ? 'positive' : 'warning'}>
            {autoDonate ? 'Active' : 'Paused'}
          </Badge>
          <Box css={{ font: 'caption', color: 'secondary' }}>
            Rate: {donationPct}%
          </Box>
        </Box>

        <Divider />

        {/* Key Metrics */}
        <Box css={{ stack: 'y', gap: 'small' }}>
          <Box css={{ font: 'subheading' }}>Donation Summary</Box>

          <Box
            css={{
              padding: 'medium',
              backgroundColor: 'container',
              borderRadius: 'medium',
            }}
          >
            <Box css={{ layout: 'row', alignX: 'spread' }}>
              <Box css={{ stack: 'y' }}>
                <Box css={{ font: 'caption', color: 'secondary' }}>
                  Total Donated
                </Box>
                <Box css={{ font: 'heading' }}>
                  ${totalDonated.toLocaleString('en-US', {
                    minimumFractionDigits: 2,
                  })}
                </Box>
              </Box>
              <Box css={{ stack: 'y', alignX: 'end' }}>
                <Box css={{ font: 'caption', color: 'secondary' }}>Today</Box>
                <Box css={{ font: 'heading' }}>
                  {txToday} tx{txToday !== 1 ? 's' : ''}
                </Box>
              </Box>
            </Box>
          </Box>

          <Box css={{ layout: 'row', gap: 'small' }}>
            <Box
              css={{
                padding: 'medium',
                backgroundColor: 'container',
                borderRadius: 'medium',
                width: 'fill',
              }}
            >
              <Box css={{ font: 'caption', color: 'secondary' }}>
                Total Transactions
              </Box>
              <Box css={{ font: 'body', fontWeight: 'bold' }}>{totalTx}</Box>
            </Box>
            <Box
              css={{
                padding: 'medium',
                backgroundColor: 'container',
                borderRadius: 'medium',
                width: 'fill',
              }}
            >
              <Box css={{ font: 'caption', color: 'secondary' }}>
                Charities Supported
              </Box>
              <Box css={{ font: 'body', fontWeight: 'bold' }}>
                {activeCharities}
              </Box>
            </Box>
          </Box>
        </Box>

        {/* Donations by Category */}
        {stats?.donation_by_category &&
          stats.donation_by_category.length > 0 && (
            <>
              <Divider />
              <Box css={{ stack: 'y', gap: 'small' }}>
                <Box css={{ font: 'subheading' }}>By Category</Box>
                <List>
                  {stats.donation_by_category.map((cat) => (
                    <ListItem
                      key={cat.category}
                      title={<Box>{cat.category}</Box>}
                      secondaryTitle={
                        <Box>
                          $
                          {cat.donated.toLocaleString('en-US', {
                            minimumFractionDigits: 2,
                          })}
                        </Box>
                      }
                    />
                  ))}
                </List>
              </Box>
            </>
          )}

        <Divider />

        {/* Quick Links */}
        <Box css={{ stack: 'y', gap: 'xsmall' }}>
          <Box css={{ font: 'caption', color: 'secondary' }}>
            Manage your donation settings, charity allocations, and download
            tax reports from the Settings page or the full Drip dashboard.
          </Box>
        </Box>
      </Box>
    </ContextView>
  );
};

export default DashboardView;
