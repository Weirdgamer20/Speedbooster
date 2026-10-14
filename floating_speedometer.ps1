# floating_speedometer.ps1 - MSI Afterburner / RTSS Style Desktop OSD Speedometer
Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase

[xml]$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="SpeedBond OSD"
        Width="310" Height="140"
        WindowStyle="None"
        AllowsTransparency="True"
        Background="Transparent"
        Topmost="True"
        ShowInTaskbar="False"
        WindowStartupLocation="Manual">
    <Border Background="#E6080C14"
            BorderBrush="#FF7700"
            BorderThickness="1.2"
            CornerRadius="8"
            Padding="12,10">
        <Border.Effect>
            <DropShadowEffect Color="#000000" BlurRadius="16" ShadowDepth="3" Opacity="0.9"/>
        </Border.Effect>
        <Grid>
            <Grid.RowDefinitions>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="*"/>
                <RowDefinition Height="Auto"/>
            </Grid.RowDefinitions>

            <!-- Title Bar (MSI Afterburner Style) -->
            <Grid Grid.Row="0" Margin="0,0,0,6">
                <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
                    <TextBlock Text="SPEEDBOND OSD" FontSize="11" FontWeight="Bold" FontFamily="Consolas, Segoe UI" Foreground="#FF7700"/>
                    <TextBlock Text=" [LIVE]" FontSize="10" FontWeight="Bold" FontFamily="Consolas, Segoe UI" Foreground="#00FF66" Margin="6,0,0,0"/>
                </StackPanel>
                <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
                    <Button Name="MiniBtn" Content="_" Width="18" Height="16" Background="Transparent"
                            BorderThickness="0" Foreground="#888888" FontSize="10" FontWeight="Bold" Cursor="Hand" Margin="0,0,4,0"/>
                    <Button Name="CloseBtn" Content="✕" Width="18" Height="16" Background="Transparent"
                            BorderThickness="0" Foreground="#FF5555" FontSize="11" FontWeight="Bold" Cursor="Hand"/>
                </StackPanel>
            </Grid>

            <!-- Main Speed Metric -->
            <Grid Grid.Row="1" Margin="0,2,0,6">
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="Auto"/>
                    <ColumnDefinition Width="*"/>
                </Grid.ColumnDefinitions>
                <TextBlock Text="DWD SPEED :" FontSize="12" FontWeight="Bold" FontFamily="Consolas, Courier New" Foreground="#00E5FF" VerticalAlignment="Center" Margin="0,0,8,0"/>
                <StackPanel Grid.Column="1" Orientation="Horizontal" VerticalAlignment="Center">
                    <TextBlock Name="SpeedDigits" Text="0.00" FontSize="26" FontWeight="ExtraBold" FontFamily="Consolas, Segoe UI" Foreground="#FFFFFF">
                        <TextBlock.Effect>
                            <DropShadowEffect Color="#00E5FF" BlurRadius="10" ShadowDepth="0" Opacity="0.6"/>
                        </TextBlock.Effect>
                    </TextBlock>
                    <TextBlock Text=" MB/s" FontSize="12" FontWeight="Bold" FontFamily="Consolas, Segoe UI" Foreground="#FF7700" VerticalAlignment="Bottom" Margin="4,0,0,3"/>
                    <TextBlock Name="MbpsLabel" Text=" (0.0 Mbps)" FontSize="10" FontFamily="Consolas, Segoe UI" Foreground="#888888" VerticalAlignment="Bottom" Margin="6,0,0,4"/>
                </StackPanel>
            </Grid>

            <!-- Dynamic Physical Adapters Breakdown -->
            <StackPanel Grid.Row="2" Name="AdaptersList" Margin="0,0,0,4">
                <TextBlock Name="LineAdapter1" Text="NET 1: Detecting..." FontSize="11" FontFamily="Consolas, Courier New" Foreground="#CCCCCC"/>
                <TextBlock Name="LineAdapter2" Text="NET 2: Detecting..." FontSize="11" FontFamily="Consolas, Courier New" Foreground="#CCCCCC" Margin="0,2,0,0"/>
            </StackPanel>

            <!-- Footer: Data Transferred -->
            <Grid Grid.Row="3" Margin="0,4,0,0">
                <TextBlock Name="TotalDataLabel" Text="TOTAL DATA: 0.0 MB" FontSize="9" FontFamily="Consolas, Courier New" Foreground="#777777"/>
                <TextBlock Text="DOUBLE-CLICK TO COMPACT" FontSize="8" FontFamily="Consolas, Segoe UI" Foreground="#555555" HorizontalAlignment="Right"/>
            </Grid>
        </Grid>
    </Border>
</Window>
"@

$reader = (New-Object System.Xml.XmlNodeReader $xaml)
$window = [System.Windows.Markup.XamlReader]::Load($reader)

# Default to top-right corner of primary screen
$screenWidth = [System.Windows.SystemParameters]::PrimaryScreenWidth
$window.Left = [Math]::Max(40, $screenWidth - 340)
$window.Top = 40

$speedDigits = $window.FindName("SpeedDigits")
$mbpsLabel = $window.FindName("MbpsLabel")
$lineAdapter1 = $window.FindName("LineAdapter1")
$lineAdapter2 = $window.FindName("LineAdapter2")
$totalDataLabel = $window.FindName("TotalDataLabel")
$closeBtn = $window.FindName("CloseBtn")
$miniBtn = $window.FindName("MiniBtn")
$adaptersList = $window.FindName("AdaptersList")

# Drag window anywhere
$window.Add_MouseLeftButtonDown({
    $window.DragMove()
})

# Double-click to toggle compact / expanded view
$isCompact = $false
$window.Add_MouseDoubleClick({
    $script:isCompact = -not $script:isCompact
    if ($script:isCompact) {
        $adaptersList.Visibility = [System.Windows.Visibility]::Collapsed
        $window.Height = 76
    } else {
        $adaptersList.Visibility = [System.Windows.Visibility]::Visible
        $window.Height = 140
    }
})

$miniBtn.Add_Click({
    $script:isCompact = -not $script:isCompact
    if ($script:isCompact) {
        $adaptersList.Visibility = [System.Windows.Visibility]::Collapsed
        $window.Height = 76
    } else {
        $adaptersList.Visibility = [System.Windows.Visibility]::Visible
        $window.Height = 140
    }
})

$closeBtn.Add_Click({
    $window.Close()
})

# 500ms Poller
$timer = New-Object System.Windows.Threading.DispatcherTimer
$timer.Interval = [TimeSpan]::FromMilliseconds(500)
$timer.Add_Tick({
    try {
        $stats = Invoke-RestMethod -Uri "http://localhost:5000/api/stats" -TimeoutSec 1 -ErrorAction SilentlyContinue
        if ($stats) {
            $totalMB = [double]$stats.total_speed_mbytes_sec
            $totalMbps = [double]$stats.total_speed_mbps
            $totalDataMB = ([double]$stats.total_bytes_downloaded / (1024 * 1024))

            $speedDigits.Text = $totalMB.ToString("0.00")
            $mbpsLabel.Text = (" ({0:0.0} Mbps)" -f $totalMbps)
            $totalDataLabel.Text = ("TOTAL DATA: {0:0.1} MB" -f $totalDataMB)

            if ($stats.interfaces) {
                # Format Adapter 1
                if ($stats.interfaces.Count -ge 1) {
                    $i1 = $stats.interfaces[0]
                    $s1 = [double]$i1.speed_mbytes_sec
                    $name1 = if ($i1.label -match '\((.*?)\)') { $matches[1] } else { $i1.name }
                    if ($name1.Length -gt 16) { $name1 = $name1.Substring(0, 16) }
                    $lineAdapter1.Text = ("{0,-16}: {1,5:0.00} MB/s [{2:0}ms]" -f $name1, $s1, [double]$i1.latency_ms)
                }
                # Format Adapter 2
                if ($stats.interfaces.Count -ge 2) {
                    $i2 = $stats.interfaces[1]
                    $s2 = [double]$i2.speed_mbytes_sec
                    $name2 = if ($i2.label -match '\((.*?)\)') { $matches[1] } else { $i2.name }
                    if ($name2.Length -gt 16) { $name2 = $name2.Substring(0, 16) }
                    $lineAdapter2.Text = ("{0,-16}: {1,5:0.00} MB/s [{2:0}ms]" -f $name2, $s2, [double]$i2.latency_ms)
                }
            }
        }
    } catch {}
})

$timer.Start()
$window.ShowDialog() | Out-Null
