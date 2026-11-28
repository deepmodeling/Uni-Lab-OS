using System;
using System.Threading.Tasks;

public class MockGripper
{
    private double _position = 0.0;
    public double Position 
    { 
        get 
        {
            _position += 1.0; 
            return _position;
        }
        private set 
        {
            _position = value;
        }
    }

    private double _velocity = 2.0;
    public double Velocity 
    { 
        get 
        {
            _velocity += 1.0; 
            return _velocity;
        }
        private set 
        {
            _velocity = value;
        }
    }

    private double _torque = 0.0;
    public double Torque 
    { 
        get 
        {
            _torque += 1.0; 
            return _torque;
        }
        private set 
        {
            _torque = value;
        }
    }

    public string Status { get; private set; } = "Idle";

    public async Task PushToAsync(double position, double torque, double velocity = 0.0)
    {
        Status = "Running";
        double currentPos = Position;

        if (velocity == 0.0)
        {
            velocity = Velocity;
        }

        double moveTime = Math.Abs(position - currentPos) / velocity;

        for (int i = 0; i < 20; i++)
        {
            Position = currentPos + (position - currentPos) / 20 * (i + 1);
            Torque = torque / (20 - i);
            Velocity = velocity;
            await Task.Delay((int)(moveTime * 1000 / 20)); // Convert seconds to milliseconds
        }

        Torque = torque;
        Status = "Idle";
    }
}